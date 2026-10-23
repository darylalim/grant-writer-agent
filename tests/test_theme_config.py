"""What `.streamlit/config.toml` must keep true.

The theme is the app's whole styling story, and every way it breaks is quiet.
Streamlit does not reject an unknown theme key: it logs
`"theme.light.baseFontSize" is not a valid config option` to the *server* log --
which nobody is watching during a run -- and drops the value. The page still
renders, one step smaller or one border short, and no test would otherwise
notice. Three separate mistakes land there:

- moving one of the top-level-only options (seven as of Streamlit 1.62: `base`,
  `baseFontSize`, `baseFontWeight`, `fontFaces`, the two metric-value options,
  `showSidebarBorder`) into a per-mode section, which a plausible
  tidy-up does while making the file look *more* consistent;
- a typo in any key;
- an upstream rename on a Streamlit upgrade.

`test_every_theme_key_is_a_real_config_option` catches all three offline.

The rest pin decisions whose rationale lives in the file's comments, because a
comment cannot fail. Contrast figures are recomputed here rather than quoted, so
a colour edit that breaks a stated guarantee fails instead of merely making the
comment wrong.
"""

from __future__ import annotations

import colorsys
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit import config as st_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / ".streamlit" / "config.toml"


def _theme() -> dict:
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))["theme"]


# Read from the file rather than restated, so every contrast check below
# measures whatever the page actually is.
LIGHT_PAGE = _theme()["light"]["backgroundColor"]
DARK_PAGE = _theme()["dark"]["backgroundColor"]


def _flatten(theme: dict, prefix: str = "theme") -> dict[str, object]:
    """Every `section.key` in the file, as Streamlit's option registry names it."""
    flat: dict[str, object] = {}
    for key, value in theme.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}.{key}"))
        else:
            flat[f"{prefix}.{key}"] = value
    return flat


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _composite(fg: str, bg: str, alpha: float) -> str:
    """Blend `fg` at `alpha` over `bg`, the way the browser composites a wash."""
    f = [int(fg.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    b = [int(bg.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(
        f"{round(alpha * f[i] + (1 - alpha) * b[i]):02X}" for i in range(3)
    )


def test_every_theme_key_is_a_real_config_option() -> None:
    """No key is silently dropped.

    This is the one that matters. A misplaced or misspelled theme key is a
    server-log line and a missing value, never an exception, so without this the
    file can degrade while the suite stays green.
    """
    st_config.get_config_options()
    valid = set(st_config._config_options_template)
    unknown = sorted(k for k in _flatten(_theme()) if k not in valid)
    assert not unknown, (
        f"not valid Streamlit config options: {unknown}. Streamlit drops these "
        "with only a server-log warning. Note some options are valid ONLY in the "
        "top-level [theme] section -- see the comment in .streamlit/config.toml."
    )


def test_top_level_only_options_stay_top_level() -> None:
    """The specific trap the file's comment describes, pinned by name.

    Named explicitly rather than left to the check above so the failure says
    *why* the key is invalid where it was put, which is the non-obvious half.
    """
    st_config.get_config_options()
    top_only = {
        name.rpartition(".")[2]
        for name in st_config._config_options_template
        if name.startswith("theme.") and name.count(".") == 1
    } - {
        name.rpartition(".")[2]
        for name in st_config._config_options_template
        if name.startswith("theme.light.") and name.count(".") == 2
    }
    assert "baseFontSize" in top_only and "showSidebarBorder" in top_only
    theme = _theme()
    for section in ("light", "dark", "sidebar"):
        misplaced = sorted(top_only & set(theme.get(section, {})))
        assert not misplaced, f"[theme.{section}] cannot hold {misplaced}"


def test_both_modes_are_defined() -> None:
    """Deleting either mode section does not restore the pre-split behaviour.

    The settings-menu switch appears when *either* section exists, and with only
    [theme.light] present Streamlit builds its Dark entry from its own stock
    palette -- a #0E1117 page with none of these surfaces, which a dark desktop
    then gets by default. So "just delete [theme.dark]" is a worse state than
    either the split or the original, and the real rollback is to fold
    [theme.light] back into [theme] and drop both dark sections.
    """
    theme = _theme()
    assert "light" in theme and "dark" in theme
    assert "sidebar" in theme["light"] and "sidebar" in theme["dark"]


def test_body_text_and_links_clear_aa_on_both_pages() -> None:
    theme = _theme()
    for mode, page in (("light", LIGHT_PAGE), ("dark", DARK_PAGE)):
        section = theme[mode]
        for token in ("textColor", "linkColor"):
            ratio = _contrast(section[token], page)
            assert ratio >= 4.5, f"[theme.{mode}] {token} is {ratio:.2f}:1 on {page}"


def test_dark_primary_serves_both_of_its_jobs() -> None:
    """`primaryColor` is a button fill AND a selected-control foreground.

    Streamlit hardcodes primary-button labels to white and draws a lit segmented
    control in `primaryColor` over a 10% wash of itself, so the token is pinned
    from both sides at once. No value clears AA on both (the curves cross near
    4.4:1 on any near-black page, whatever the hue), so this asserts the split
    actually chosen: AA on the button label, above the 3:1 large-text floor on
    the segment -- on the sidebar too, whose lighter surface eats into the
    segment's margin.
    """
    theme = _theme()
    for section in (theme["dark"], theme["dark"]["sidebar"]):
        primary = section["primaryColor"]
        surface = section["backgroundColor"]
        label = _contrast("#FFFFFF", primary)
        segment = _contrast(primary, _composite(primary, surface, 0.10))
        assert label >= 4.5, f"white button label on {primary} is {label:.2f}:1"
        assert segment >= 3.0, (
            f"lit segment on its wash over {surface} is {segment:.2f}:1"
        )


def _hue(hex_colour: str) -> float:
    rgb = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    return colorsys.rgb_to_hls(*rgb)[0] * 360


def test_light_and_dark_share_one_accent() -> None:
    """The modes are one design mirrored, not two unrelated palettes.

    Dark lifts the accent's lightness so it stays vivid on a dark page, and has
    to search for it (see the test above), but the hue is the identity: the
    primary action is "the indigo one" in either mode. A dark primary drifted to
    another hue still passes every contrast check here, which is why this is
    pinned separately. Each sidebar uses its own mode's accent unchanged.
    """
    theme = _theme()
    light, dark = theme["light"], theme["dark"]
    assert light["sidebar"]["primaryColor"] == light["primaryColor"]
    assert dark["sidebar"]["primaryColor"] == dark["primaryColor"]
    drift = abs(_hue(light["primaryColor"]) - _hue(dark["primaryColor"]))
    assert drift <= 10, (
        f"light {light['primaryColor']} and dark {dark['primaryColor']} differ "
        f"by {drift:.1f} degrees of hue"
    )


def test_input_borders_clear_three_to_one_on_every_surface() -> None:
    """Pins decision 7 of the Track F plan: input outlines pass WCAG 1.4.11.

    An input is identified by its outline, and Streamlit draws that outline and
    every decorative `st.container(border=True)` from the one `borderColor`, so
    the heavier boxes are the price of findable fields -- chosen deliberately
    (see the borders note in the config). A tidy-up that lightens the boxes back
    to the template's 1.27:1 looks like an improvement and fails here instead.
    Each section's border is measured against both fills an input can sit on
    in that section, the page and the secondary surface.
    """
    theme = _theme()
    for mode in ("light", "dark"):
        for where, section in (("", theme[mode]), (".sidebar", theme[mode]["sidebar"])):
            border = section["borderColor"]
            for token in ("backgroundColor", "secondaryBackgroundColor"):
                ratio = _contrast(border, section[token])
                assert ratio >= 3.0, (
                    f"[theme.{mode}{where}] borderColor {border} is {ratio:.2f}:1 "
                    f"on {token} {section[token]}"
                )


def test_focused_input_border_clears_three_to_one_on_every_surface() -> None:
    """A focused input redraws its border in solid `primaryColor`.

    Observed by tabbing through the running app (Streamlit 1.66): the Reason
    input's border computed to `primaryColor` exactly. Buttons draw their ring
    differently -- see the next test, which is the exception to this one.
    """
    theme = _theme()
    for mode in ("light", "dark"):
        for where, section in (("", theme[mode]), (".sidebar", theme[mode]["sidebar"])):
            ring = section["primaryColor"]
            for token in ("backgroundColor", "secondaryBackgroundColor"):
                ratio = _contrast(ring, section[token])
                assert ratio >= 3.0, (
                    f"[theme.{mode}{where}] focus ring {ring} is {ratio:.2f}:1 "
                    f"on {token} {section[token]}"
                )


# Measured 2026-10-22, recorded here so a change to either is a decision.
BUTTON_RING_RATIOS = {
    ("light", "backgroundColor"): 2.50,
    ("light", "sidebar.backgroundColor"): 2.48,
    ("dark", "backgroundColor"): 1.82,
    ("dark", "sidebar.backgroundColor"): 1.81,
}


def test_button_focus_ring_is_a_documented_exception() -> None:
    """A focused button's ring is under 3:1, and no theme value can fix it.

    Streamlit draws it as a box-shadow of `primaryColor` at a fixed 50% alpha
    (observed: `rgba(96, 94, 239, 0.5) 0 0 0 2.8px` on the dark page), so the
    ring sits halfway between the accent and the page. Raising it to 3:1 on the
    dark page needs a primary too light for the white label Streamlit hardcodes
    on the button -- the same squeeze as the label/segment split above -- and
    CSS is ruled out for this app. Accepted as an exception in Track F: the
    focused button also darkens its fill (to #2F2791 in light), a second cue the
    ring does not depend on.

    Pinned at the measured ratios, not at a floor, so a palette change that moves
    them is noticed and re-decided rather than drifting. If Streamlit makes the
    ring solid, the previous test's 3:1 applies to buttons too: delete this one.
    """
    theme = _theme()
    for (mode, where), expected in BUTTON_RING_RATIOS.items():
        section = theme[mode]["sidebar"] if where.startswith("sidebar") else theme[mode]
        surface = section["backgroundColor"]
        ring = _composite(section["primaryColor"], surface, 0.5)
        ratio = _contrast(ring, surface)
        assert round(ratio, 2) == expected, (
            f"[theme.{mode}] button focus ring on {where} is now {ratio:.2f}:1, "
            f"recorded as {expected}. Re-decide the exception, then update it."
        )


def test_heading_font_is_set_where_streamlit_reads_it() -> None:
    """Serif headings are the third of Track F's three changes.

    Kept in [theme] like `font` and `codeFont`, since typography does not depend
    on the mode. If the screenshots show the serif reads as fussy at 14px, the
    plan's fallback is to drop it -- delete this test in the same commit, which
    is the point: it makes that a decision rather than a quiet regression.
    """
    theme = _theme()
    assert "Source Serif 4" in theme["headingFont"]
    for section in ("light", "dark"):
        assert "headingFont" not in theme[section]


def test_dark_mirrors_the_light_half_s_surface_boundaries() -> None:
    """Every boundary that reads in light must still read in dark.

    A border equal to the fill it encloses is an invisible boundary, and the
    layout is built almost entirely from `st.container(border=True)`. Stated as a
    *mirror* of the light half rather than as blanket distinctness, because the
    light half is where the design decides which boundaries exist; dark's job is
    not to lose one. That framing dates from the zinc template, whose light
    sidebar set `codeBackgroundColor` and `borderColor` to the same zinc-200 (the
    stone redesign separates them), and it caught a real regression: an earlier
    dark half used zinc-800 for `borderColor`, colliding with
    `codeBackgroundColor` in [theme.dark] and `secondaryBackgroundColor` in
    [theme.dark.sidebar], both of which are distinct in light.
    """
    theme = _theme()
    tokens = (
        "backgroundColor",
        "secondaryBackgroundColor",
        "codeBackgroundColor",
        "borderColor",
    )
    for name, light, dark in (
        ("theme", theme["light"], theme["dark"]),
        ("theme.*.sidebar", theme["light"]["sidebar"], theme["dark"]["sidebar"]),
    ):
        for i, a in enumerate(tokens):
            for b in tokens[i + 1 :]:
                if a not in light or b not in light:
                    continue
                if light[a] != light[b]:
                    assert dark[a] != dark[b], (
                        f"[{name}] {a} and {b} are distinct in light "
                        f"({light[a]} vs {light[b]}) but both {dark[a]} in dark"
                    )


def test_chart_ramp_clears_three_to_one_on_both_pages() -> None:
    """The reason the ramp left the zinc template's.

    `chartCategoricalColors` is set once, in [theme], so one ramp serves both modes
    and every entry has to clear the non-text-graphics floor on each page. (Since
    Streamlit 1.62 it could be set per mode; the config's comment says why it
    isn't.) The template's zinc-forward ramp did not. On the stone-950 page the
    worst entry clears by only 0.05, so a darker dark page fails here first.
    """
    ramp = _theme()["chartCategoricalColors"]
    assert len(ramp) == 7
    for colour in ramp:
        for page in (LIGHT_PAGE, DARK_PAGE):
            ratio = _contrast(colour, page)
            assert ratio >= 3.0, f"{colour} is {ratio:.2f}:1 on {page}"
