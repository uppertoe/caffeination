---
name: coffee-taxonomy
description: Use when modelling, rendering, or validating coffee drinks in coffee-rch — defines the Melbourne drink set, the option axes (base/size/milk/shots/temp/sweetener), and the constraint matrix (e.g. espresso has no size, long black has no milk). Reach for this skill any time the drink-builder, the menu enums, or the till summary changes.
---

# coffee-taxonomy

The drink-builder UX in `coffee-rch` is the heart of the app. Getting the
taxonomy right is what makes the form feel native to a Melbourne café
regular instead of a generic Starbucks clone. This skill captures the
domain so we don't relitigate it in every PR.

## The option axes

A drink is a **base** + an optional set of **modifiers**. The modifiers
that apply depend on the base. The axes are:

| Axis        | Values                                                    |
| ----------- | --------------------------------------------------------- |
| `temp`      | `hot`, `iced`                                              |
| `size`      | `small` (~240 ml), `regular` (~350 ml), `large` (~450 ml)  |
| `milk`      | `full_cream`, `skim`, `oat`, `soy`, `almond`, `lactose_free` |
| `shots`     | `1`, `2`, `3` (rare)                                      |
| `sweetener` | `none`, `one_sugar`, `two_sugar`, `raw`, `honey`           |
| `strength`  | `regular`, `weak`, `extra_shot`, `decaf`, `half_caf`       |
| `notes`     | free text (≤80 chars)                                     |

## The drinks

| Id              | Display name      | Family    | Has milk? | Sized? | Shot-adjustable? | Notes |
| --------------- | ----------------- | --------- | --------- | ------ | ---------------- | ----- |
| `espresso`      | Espresso          | espresso  | no        | no     | yes (1 or 2)     | "Double" = 2 shots. No size. No milk. |
| `macchiato`     | Macchiato         | espresso  | dash      | no     | yes (1 or 2)     | Short (1 shot) or long (2 shots, served in a taller glass). The shot count *is* the size — never render a separate size selector. Naming: render as `short macchiato` / `long macchiato`, not "double macchiato". |
| `piccolo`       | Piccolo (latte)   | espresso  | yes       | no     | 1 only           | Ristretto + steamed milk in a 90 ml glass. No size; no shot adjust. |
| `long_black`    | Long black        | black     | no        | yes    | yes              | Hot water + 2 shots. No milk. Sized. |
| `latte`         | Latte             | milk      | yes       | yes    | yes              | The default group-order workhorse. |
| `flat_white`    | Flat white        | milk      | yes       | yes    | yes              | Less foam than latte; same axes. |
| `cappuccino`    | Cappuccino        | milk      | yes       | yes    | yes              | Choc dust on top. |
| `magic`         | Magic             | milk      | yes       | no     | 2 only           | Melbourne specialty: double ristretto + steamed milk in a 150 ml tulip. Fixed size, fixed shots. |
| `mocha`         | Mocha             | milk+choc | yes       | yes    | yes              | Espresso + chocolate + milk. |
| `hot_chocolate` | Hot chocolate     | choc      | yes       | yes    | no               | No coffee. Hide shots and strength. |
| `chai_latte`    | Chai latte        | spice     | yes       | yes    | no               | No coffee. Hide shots and strength. |
| `dirty_chai`    | Dirty chai        | spice+esp | yes       | yes    | yes              | Chai latte + 1–2 shots. |
| `matcha_latte`  | Matcha latte      | tea       | yes       | yes    | no               | No coffee. |

**Iced variants.** Anything with `family in {milk, milk+choc, choc, spice, tea, black}`
supports `temp = iced`. Espresso-family drinks are hot-only in the scaffold
to keep the form sane (iced macchiato exists but is rarely ordered here).

## The constraint matrix — how the form should behave

Drive these off a per-drink rule object, not template `if`s scattered
around. Concretely each drink has flags like:

```python
@dataclass(frozen=True)
class Drink:
    id: str
    display: str
    family: Family
    sized: bool
    milk: MilkPolicy        # NONE | DASH | REQUIRED
    shot_choices: tuple[int, ...]   # () means "not adjustable"
    allows_strength: bool
    allows_iced: bool
```

When the user picks a base in the form:

- If `not sized`: hide the size selector entirely; the till summary just
  uses the drink name (e.g. "double espresso", not "regular double espresso").
- If `milk is NONE`: hide milk + show a small note (`"served black"`).
- If `milk is DASH`: hide milk and the strength axis (it's a dash, not a
  base for customisation).
- If `shot_choices` is empty: hide the shot selector; otherwise render it
  as a segmented control with those values.
- If `not allows_strength`: hide the strength axis.
- If `not allows_iced`: hide the hot/iced toggle, force `hot`.
- The Alpine model on the form holds the chosen `base.id` and computes
  `visible` flags from a JSON-serialised copy of the rules; the server
  re-validates on submit using the same Python rules object.

## Till summary — how groups collapse

Two saved drinks merge into a single line in the till summary iff every
**ordered** option matches: base, size, milk, shots, strength, temp,
sweetener. Free-text notes never merge — each noted drink is its own line.

Format the line as: `<count>x <size?> <strength?> <temp?> <milk?> <drink>` with
notes appended in brackets. Examples:

- `2x small latte`
- `1x large oat flat white`
- `1x double espresso`
- `1x iced regular soy mocha (no sugar, light ice)`
- `1x magic`

Drop axes that are at their default for that drink — don't say "1x hot
regular full-cream latte" when "1x regular latte" is what someone says
out loud.

## Naming conventions

- **Stable ids** (`latte`, `flat_white`, `oat`) are forever — they're
  persisted on orders.
- **Display strings** are sentence case in the UI ("Flat white") and
  lower case in the till summary ("flat white"), matching how a barista
  writes them on a cup.
- Avoid US terms (`drip`, `cold brew`, `frappuccino`) — they don't belong
  on a Melbourne menu.

## When extending

Adding a new drink:
1. Add the `Drink(...)` entry to the menu module.
2. If it introduces a new milk or sweetener, add the enum value first.
3. Decide its constraint flags up front — don't paper them over in
   templates.
4. Add a test that the till summary collapses two copies into one line
   and that the form hides the right axes.
