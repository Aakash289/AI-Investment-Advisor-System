"""
app/theme.py: the app's colours, fonts and page styles (build step 16).

What this file does:
    Holds the colour palette in one place, so the Gradio theme, the result card and the
    charts all agree. The palette is built around the deep navy already used in the PDF
    report and its charts (#1f3a5f), on a warm paper-white background, with three muted
    signal colours for the verdicts:

        navy    #1f3a5f   buttons, headings, the share price line
        paper   #f6f4ef   page background (a little warmer than plain white)
        card    #ffffff   panels
        line    #e2ddd3   borders and dividers
        ink     #1b2533   main text
        muted   #667085   secondary text
        green   #2f7d5b   favourable
        amber   #b7791f   neutral
        brick   #b4413a   unfavourable

    The signal colours are deliberately soft: a verdict is a summary of evidence, not a
    flashing buy or sell sign. Each has a pale background for its badge. A dark set is
    defined too, used when the computer is in dark mode.

    THEME is the Gradio theme (gr.themes.Base with these colours); CSS styles the
    result card, the progress list and the score bars drawn in app/view.py.
"""

from __future__ import annotations

# gradio: the web app library. gr.themes builds a theme from colour scales and fonts.
import gradio as gr

PALETTE = {
    "navy": "#1f3a5f", "navy_hover": "#2b4e7c", "navy_soft": "#e8eef5",
    "paper": "#f6f4ef", "card": "#ffffff", "line": "#e2ddd3",
    "ink": "#1b2533", "muted": "#667085",
    "green": "#2f7d5b", "green_soft": "#e6f2ec",
    "amber": "#b7791f", "amber_soft": "#fbf1df",
    "brick": "#b4413a", "brick_soft": "#f8e7e5",
}
DARK = {
    "navy": "#8fb3e0", "navy_soft": "#1f2c40",
    "paper": "#11161e", "card": "#19212d", "line": "#2a3546",
    "ink": "#e6ebf2", "muted": "#9aa6b8",
    "green": "#5bbf8f", "green_soft": "#16302a",
    "amber": "#e0a84a", "amber_soft": "#33291a",
    "brick": "#e27a72", "brick_soft": "#3a1f1f",
}


# --- The Gradio theme -------------------------------------------------------------------------

# A navy colour scale (light to dark) for Gradio's "primary" hue; c500 is the brand navy's
# lighter cousin and c700 the navy itself, which is what buttons use below.
NAVY = gr.themes.Color(
    name="navy", c50="#eef3f9", c100="#dbe5f1", c200="#b8cbe2", c300="#8fadd0", c400="#5f88b8",
    c500="#3d6898", c600="#2b4e7c", c700="#1f3a5f", c800="#172c48", c900="#101f33", c950="#0a1422",
)

THEME = gr.themes.Base(
    primary_hue=NAVY,
    secondary_hue=gr.themes.colors.stone,
    neutral_hue=gr.themes.colors.stone,
    radius_size=gr.themes.sizes.radius_md,
    # Inter from Google Fonts, falling back to the system font if offline.
    font=[gr.themes.GoogleFont("Inter"), "system-ui", "Segoe UI", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "Consolas", "monospace"],
).set(
    body_background_fill=PALETTE["paper"], body_background_fill_dark=DARK["paper"],
    body_text_color=PALETTE["ink"], body_text_color_dark=DARK["ink"],
    body_text_color_subdued=PALETTE["muted"], body_text_color_subdued_dark=DARK["muted"],
    background_fill_primary=PALETTE["card"], background_fill_primary_dark=DARK["card"],
    background_fill_secondary=PALETTE["paper"], background_fill_secondary_dark=DARK["paper"],
    block_background_fill=PALETTE["card"], block_background_fill_dark=DARK["card"],
    block_border_color=PALETTE["line"], block_border_color_dark=DARK["line"],
    border_color_primary=PALETTE["line"], border_color_primary_dark=DARK["line"],
    block_label_text_color=PALETTE["muted"], block_label_text_color_dark=DARK["muted"],
    button_primary_background_fill=PALETTE["navy"], button_primary_background_fill_dark="#2b4e7c",
    button_primary_background_fill_hover=PALETTE["navy_hover"], button_primary_background_fill_hover_dark="#3d6898",
    button_primary_text_color="#ffffff", button_primary_text_color_dark="#ffffff",
    button_secondary_background_fill=PALETTE["card"], button_secondary_background_fill_dark=DARK["card"],
    button_secondary_border_color=PALETTE["line"], button_secondary_border_color_dark=DARK["line"],
    input_background_fill=PALETTE["card"], input_background_fill_dark=DARK["card"],
    color_accent_soft=PALETTE["navy_soft"], color_accent_soft_dark=DARK["navy_soft"],
)


# --- Page styles ------------------------------------------------------------------------------

def _vars(p: dict) -> str:
    return "".join(f"--isa-{k.replace('_', '-')}: {v};" for k, v in p.items())


# Gradio puts the class "dark" on the page in dark mode, so the same names switch colour.
CSS = f"""
:root {{ {_vars(PALETTE)} }}
.dark {{ {_vars({**PALETTE, **DARK})} }}

.gradio-container {{ max-width: 1400px !important; margin: 0 auto; }}

/* Header */
.isa-header {{ display: flex; align-items: baseline; gap: 14px; padding: 6px 2px 2px; }}
.isa-header h1 {{ margin: 0; font-size: 1.45rem; color: var(--isa-navy); font-weight: 700; letter-spacing: -0.01em; }}
.isa-header span {{ color: var(--isa-muted); font-size: 0.92rem; }}

/* Result card */
.isa-card {{ background: var(--isa-card); border: 1px solid var(--isa-line); border-radius: 12px;
            padding: 18px 20px; color: var(--isa-ink); }}
.isa-card h2 {{ margin: 0 0 2px; font-size: 1.15rem; color: var(--isa-ink); }}
.isa-sub {{ color: var(--isa-muted); font-size: 0.85rem; margin-bottom: 12px; }}
.isa-badges {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 14px; }}
.isa-badge {{ padding: 4px 12px; border-radius: 999px; font-weight: 600; font-size: 0.9rem; }}
.isa-favourable {{ background: var(--isa-green-soft); color: var(--isa-green); }}
.isa-neutral {{ background: var(--isa-amber-soft); color: var(--isa-amber); }}
.isa-unfavourable {{ background: var(--isa-brick-soft); color: var(--isa-brick); }}
.isa-other {{ background: var(--isa-navy-soft); color: var(--isa-navy); }}
.isa-conf {{ background: transparent; border: 1px solid var(--isa-line); color: var(--isa-muted); font-weight: 500; }}

.isa-amount {{ font-size: 1.05rem; font-weight: 600; color: var(--isa-ink); margin: 4px 0; }}
.isa-label {{ text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.72rem; color: var(--isa-muted);
             margin-top: 14px; margin-bottom: 4px; font-weight: 600; }}
.isa-card ul {{ margin: 4px 0 0 18px; padding: 0; }}
.isa-card li {{ margin: 2px 0; font-size: 0.9rem; }}
.isa-note {{ color: var(--isa-muted); font-size: 0.82rem; margin-top: 10px; line-height: 1.4; }}

/* Score bars: -2 on the left, +2 on the right, a tick at 0 */
.isa-bars {{ display: grid; grid-template-columns: 150px 1fr 48px; gap: 6px 10px; align-items: center; }}
.isa-bars .name {{ font-size: 0.86rem; color: var(--isa-ink); }}
.isa-bars .val {{ font-size: 0.86rem; text-align: right; font-variant-numeric: tabular-nums; color: var(--isa-muted); }}
.isa-track {{ position: relative; height: 8px; background: var(--isa-paper); border: 1px solid var(--isa-line);
             border-radius: 999px; }}
.isa-track .zero {{ position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--isa-muted); }}
.isa-track .fill {{ position: absolute; top: 0; bottom: 0; border-radius: 999px; }}
.isa-track .fill.pos {{ background: var(--isa-green); }}
.isa-track .fill.neg {{ background: var(--isa-brick); }}
.isa-track .fill.total {{ background: var(--isa-navy); }}
.isa-missing {{ color: var(--isa-muted); font-style: italic; }}

/* Progress list while a run is going */
.isa-progress {{ background: var(--isa-card); border: 1px solid var(--isa-line); border-radius: 12px; padding: 14px 18px; }}
.isa-progress .head {{ font-weight: 600; color: var(--isa-navy); margin-bottom: 8px; }}
.isa-step {{ display: flex; gap: 10px; align-items: center; padding: 3px 0; font-size: 0.92rem; color: var(--isa-muted); }}
.isa-step .dot {{ width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--isa-line); flex: none; }}
.isa-step.done {{ color: var(--isa-ink); }}
.isa-step.done .dot {{ background: var(--isa-green); border-color: var(--isa-green); }}
.isa-step.running {{ color: var(--isa-navy); font-weight: 600; }}
.isa-step.running .dot {{ border-color: var(--isa-navy); animation: isa-pulse 1s ease-in-out infinite; }}
.isa-step .secs {{ margin-left: auto; font-variant-numeric: tabular-nums; font-size: 0.82rem; color: var(--isa-muted); font-weight: 400; }}
@keyframes isa-pulse {{ 0%, 100% {{ background: transparent; }} 50% {{ background: var(--isa-navy); }} }}

/* Empty state */
.isa-empty {{ border: 1px dashed var(--isa-line); border-radius: 12px; padding: 28px 22px; color: var(--isa-muted);
             text-align: center; line-height: 1.5; }}
.isa-empty b {{ color: var(--isa-navy); }}

/* Disclaimer strip, always visible */
.isa-disclaimer {{ background: var(--isa-navy-soft); color: var(--isa-navy); border-radius: 10px; padding: 8px 14px;
                  font-size: 0.85rem; }}

/* Tables in the side tabs */
.isa-table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; border: none !important; }}
.isa-table tr, .isa-table th, .isa-table td {{ border-left: none !important; border-right: none !important;
                                              border-top: none !important; background: transparent !important; }}
.isa-table th {{ text-align: left; color: var(--isa-muted); font-weight: 600; border-bottom: 1px solid var(--isa-line);
                padding: 6px 8px; }}
.isa-table td {{ border-bottom: 1px solid var(--isa-line); padding: 6px 8px; color: var(--isa-ink); }}
"""
