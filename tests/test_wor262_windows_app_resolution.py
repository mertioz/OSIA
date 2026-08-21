"""Focused WOR-262 Windows packaged-application resolution regressions."""

from __future__ import annotations

import inspect
import json
import subprocess

from osia import agent as agent_module
from osia.tools import desktop_control


def _start_apps(monkeypatch, rows):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(rows),
        )

    monkeypatch.setattr(desktop_control.subprocess, "run", fake_run)


def test_classic_app_paths_resolution_remains_first(monkeypatch):
    route = (r"C:\Program Files\Example\Example.exe", ["--start"])
    monkeypatch.setattr(desktop_control.sys, "platform", "win32")
    monkeypatch.setattr(desktop_control.os.path, "isabs", lambda _path: False)
    monkeypatch.setattr(
        desktop_control, "_resolve_app_paths_registry", lambda _name: route,
    )
    monkeypatch.setattr(
        desktop_control,
        "_resolve_start_menu_lnk",
        lambda _name: (_ for _ in ()).throw(AssertionError("later tier called")),
    )

    assert desktop_control.resolve_windows_app("Example") == route


def test_packaged_app_resolves_from_stable_identity_when_display_is_localized(
    monkeypatch,
):
    _start_apps(monkeypatch, [{
        "Name": "Calculatrice",
        "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
    }])

    assert desktop_control._resolve_uwp("Calculator") == (
        "explorer.exe",
        [
            "shell:AppsFolder\\"
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        ],
    )


def test_display_alias_maps_to_exact_structured_app_identity(monkeypatch):
    _start_apps(monkeypatch, [{
        "Name": "Notes localisées",
        "AppID": "Contoso.NorthwindNotes_abcd1234!Main",
    }])

    route = desktop_control._resolve_uwp("Northwind Notes")

    assert route == (
        "explorer.exe",
        ["shell:AppsFolder\\Contoso.NorthwindNotes_abcd1234!Main"],
    )


def test_equally_strong_structured_matches_fail_closed(monkeypatch):
    _start_apps(monkeypatch, [
        {"Name": "Un", "AppID": "Contoso.Notes_abcd1234!Main"},
        {"Name": "Deux", "AppID": "Fabrikam.Notes_efgh5678!Main"},
    ])

    assert desktop_control._resolve_uwp("Notes") is None


def test_duplicate_rows_for_same_identity_are_not_ambiguous(monkeypatch):
    row = {"Name": "Notes", "AppID": "Contoso.Notes_abcd1234!Main"}
    _start_apps(monkeypatch, [row, dict(row)])

    assert desktop_control._resolve_uwp("Notes") == (
        "explorer.exe",
        ["shell:AppsFolder\\Contoso.Notes_abcd1234!Main"],
    )


def test_nonexistent_packaged_app_fails_cleanly(monkeypatch):
    _start_apps(monkeypatch, [{
        "Name": "Completely Different",
        "AppID": "Contoso.Different_abcd1234!Main",
    }])

    assert desktop_control._resolve_uwp("Missing Application") is None


def test_classic_start_apps_display_route_remains_supported(monkeypatch):
    _start_apps(monkeypatch, [{
        "Name": "Example Classic",
        "AppID": r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\Example.exe",
    }])

    assert desktop_control._resolve_uwp("Example Classic") == (
        "explorer.exe",
        [
            "shell:AppsFolder\\"
            r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\Example.exe"
        ],
    )


def test_packaged_resolver_returns_launch_route_not_effect_proof(monkeypatch):
    _start_apps(monkeypatch, [{
        "Name": "Localized",
        "AppID": "Contoso.Clock_abcd1234!Main",
    }])

    route = desktop_control._resolve_uwp("Clock")

    assert route == (
        "explorer.exe",
        ["shell:AppsFolder\\Contoso.Clock_abcd1234!Main"],
    )
    assert "success" not in repr(route).casefold()
    assert "open" not in repr(route).casefold()


def test_launcher_return_code_zero_does_not_prove_application_open(monkeypatch):
    class CleanLauncherExit:
        def poll(self):
            return 0

    monkeypatch.setattr(agent_module.sys, "platform", "win32")
    monkeypatch.setattr(agent_module, "_lookup_memory_app_route", lambda _name: None)
    monkeypatch.setattr(desktop_control, "find_app_window", lambda _name: None)
    monkeypatch.setattr(
        desktop_control,
        "resolve_windows_app",
        lambda _name: (
            "explorer.exe",
            ["shell:AppsFolder\\Contoso.Example_abcd1234!Main"],
        ),
    )
    monkeypatch.setattr(
        desktop_control, "windows_app_candidates", lambda *_args: {"example"},
    )
    monkeypatch.setattr(desktop_control, "find_app_window_multi", lambda _c: None)
    monkeypatch.setattr(desktop_control, "foreground_snapshot", lambda: object())
    monkeypatch.setattr(desktop_control, "is_app_running", lambda _c: False)
    monkeypatch.setattr(desktop_control, "APP_LAUNCH_WINDOW_TIMEOUT", 0.0)
    monkeypatch.setattr(agent_module.subprocess, "Popen", lambda *_a, **_k: CleanLauncherExit())

    result = agent_module.tool_open_application("Example")

    assert result.startswith("ERROR:")
    assert "no application window appeared" in result


def test_calculator_style_identity_matching_is_generic(monkeypatch):
    _start_apps(monkeypatch, [{
        "Name": "Calendrier localisé",
        "AppID": "Example.WindowsCalendar_qwerty123!Application",
    }])

    assert desktop_control._resolve_uwp("Calendar") == (
        "explorer.exe",
        ["shell:AppsFolder\\Example.WindowsCalendar_qwerty123!Application"],
    )


def test_production_resolver_contains_no_calculator_special_case():
    source = inspect.getsource(desktop_control._packaged_app_identity_score)
    source += inspect.getsource(desktop_control._resolve_uwp)

    assert "calculator" not in source.casefold()


def test_invalid_or_unsafe_packaged_identity_is_rejected(monkeypatch):
    _start_apps(monkeypatch, [{
        "Name": "Example",
        "AppID": "Contoso.Example_abcd1234!Main\nInjected",
    }])

    assert desktop_control._resolve_uwp("Example") is None


def test_structured_identity_outranks_localized_display_guess(monkeypatch):
    _start_apps(monkeypatch, [
        {"Name": "Notes", "AppID": "Contoso.Other_abcd1234!Main"},
        {"Name": "Localized", "AppID": "Fabrikam.Notes_efgh5678!Main"},
    ])

    assert desktop_control._resolve_uwp("Notes") == (
        "explorer.exe",
        ["shell:AppsFolder\\Fabrikam.Notes_efgh5678!Main"],
    )
