"""What `.streamlit/config.toml` must keep true.

The theme is the app's whole styling story, and every way it breaks is quiet.
Streamlit does not reject an unknown theme key: it logs
`"theme.light.baseFontSize" is not a valid config option` to the *server* log --
which nobody is watching during a run -- and drops the value. The page still
renders, one step smaller or one border short, and no test would otherwise
notice. Three separate mistakes land there:

- moving one of the ten top-level-only options (`base`, `baseFontSize`,
  `baseFontWeight`, the three chart ramps, `fontFaces`, the two metric-value
  options, `showSidebarBorder`) into a per-mode section, which a plausible
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

import tomllib
from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit import config as st_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / ".streamlit" / "config.toml"

# The vendored template the light half is copied from. Lives inside the
# installed streamlit package, so it moves with the pinned version.
TEMPLATE_PATH = (
    Path(st_config.__file__).parent
    / ".agents/skills/developing-with-streamlit"
    / "assets/templates/themes/configs/shadcn.toml"
)

# The single documented deviation from that template. Adding to this set is a
# deliberate act; growing it silently is what this guards against.
DOCUMENTED_DEVIATIONS = {"chartCategoricalColors"}

LIGHT_PAGE = "#FFFFFF"
DARK_PAGE = "#09090B"


def _theme() -> dict:
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))["theme"]


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
        "with only a server-log warning. Note ten options are valid ONLY in the "
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


@pytest.mark.skipif(not TEMPLATE_PATH.is_file(), reason="vendored template not present")
def test_light_half_still_matches_the_vendored_template() -> None:
    """The light surface is the template's, so it stays diffable on an upgrade."""
    template = tomllib.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))["theme"]
    template_sidebar = template.pop("sidebar", {})
    theme = _theme()
    light = {**theme, **theme.get("light", {})}
    light_sidebar = theme.get("light", {}).get("sidebar", {})

    drift = {k for k, v in template.items() if light.get(k) != v}
    drift |= {
        f"sidebar.{k}" for k, v in template_sidebar.items() if light_sidebar.get(k) != v
    }
    assert drift == DOCUMENTED_DEVIATIONS, (
        f"drift vs shadcn.toml is {sorted(drift)}, expected "
        f"{sorted(DOCUMENTED_DEVIATIONS)}. Either revert the change or document it."
    )


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
        assert section["backgroundColor"] == page
        for token in ("textColor", "linkColor"):
            ratio = _contrast(section[token], page)
            assert ratio >= 4.5, f"[theme.{mode}] {token} is {ratio:.2f}:1 on {page}"


def test_dark_primary_serves_both_of_its_jobs() -> None:
    """`primaryColor` is a button fill AND a selected-control foreground.

    Streamlit hardcodes primary-button labels to white and draws a lit segmented
    control in `primaryColor` over a 10% wash of itself, so the token is pinned
    from both sides at once. No value clears AA on both (the curves cross near
    4.4:1 on a zinc-950 page), so this asserts the split actually chosen: AA on
    the button label, above the 3:1 large-text floor on the segment.
    """
    primary = _theme()["dark"]["primaryColor"]
    label = _contrast("#FFFFFF", primary)
    segment = _contrast(primary, _composite(primary, DARK_PAGE, 0.10))
    assert label >= 4.5, f"white button label on {primary} is {label:.2f}:1"
    assert segment >= 3.0, f"lit segment label on its wash is {segment:.2f}:1"


def test_dark_mirrors_the_light_half_s_surface_boundaries() -> None:
    """Every boundary that reads in light must still read in dark.

    A border equal to the fill it encloses is an invisible boundary, and the
    layout is built almost entirely from `st.container(border=True)`. Stated as a
    *mirror* of the light half rather than as blanket distinctness, because the
    vendored template itself sets the light sidebar's `codeBackgroundColor` and
    `borderColor` to the same zinc-200 -- so demanding all pairs differ would fail
    on a collision inherited from upstream and say nothing about this diff. What
    is this diff's business is not *introducing* one: an earlier dark half used
    zinc-800 for `borderColor`, colliding with `codeBackgroundColor` in
    [theme.dark] and `secondaryBackgroundColor` in [theme.dark.sidebar], both of
    which are distinct in light.
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
    """The reason the ramp deviates from the template at all.

    `chartCategoricalColors` is top-level-only, so one ramp serves both modes and
    every entry has to clear the non-text-graphics floor on each page. The
    template's zinc-forward ramp does not, which is what this deviation buys.
    """
    ramp = _theme()["chartCategoricalColors"]
    assert len(ramp) == 7
    for colour in ramp:
        for page in (LIGHT_PAGE, DARK_PAGE):
            ratio = _contrast(colour, page)
            assert ratio >= 3.0, f"{colour} is {ratio:.2f}:1 on {page}"
