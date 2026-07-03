"""A GUI / graphical-interface brief is a WEB app, not a python CLI.

Regression for a real reported miss: "a finance/trading app ... with all
configuration configured in gui" routed to a python CLI, because 'gui' was no
signal at all and the brief's "...apis for data" hit python's 'data' keyword. A
GUI implies a graphical/web app; an AI-bearing one bumps to nextjs so a server
route can safely hold the API keys (a react/static SPA would leak them).
"""

from __future__ import annotations

from skyn3t.studio.planner import detect_stack

_FINANCE = (
    "Build a finance app that uses ai from all type of ai models to help with "
    "automation of trading stocks and crypto with all configuration configured in "
    "gui - be able to trade super risky risky medium conservative - a ultimate "
    "trading app with bots - start out with using alpaca tie into multiple apis "
    "for data etc"
)


def test_gui_ai_trading_app_is_a_web_app_not_a_cli():
    stack = detect_stack(_FINANCE)
    # The real requirement: a web app, NOT a python CLI. It's nextjs today because
    # the AI in the brief bumps the web stack to one with a server route for keys.
    assert stack not in {"python", "cli"}, f"a GUI trading app must not be a CLI (got {stack})"
    assert stack in {"nextjs", "react", "fastapi", "static"}, f"want a web stack, got {stack}"
    assert stack == "nextjs"  # AI + gui -> fullstack (holds the Alpaca/AI keys server-side)


def test_gui_without_ai_is_a_plain_web_spa():
    assert detect_stack("a note manager with a graphical interface") == "react"


def test_gui_is_word_bounded_and_steals_no_genuine_cli_or_data_briefs():
    # 'gui' must fire only as a standalone word — never inside other words, and it
    # must not hijack genuine CLI / data / library briefs.
    assert detect_stack("a penguin facts explorer CLI") != "react"
    assert detect_stack("a bot for arguing on forums") != "react"
    assert detect_stack("a data pipeline script") == "python"
    assert detect_stack("a python library for stats") == "python"
