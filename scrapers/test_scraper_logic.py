from pathlib import Path

from scraper_template import (
    AppMonitorConfig,
    AppRuntimeState,
    IndicatorExtractor,
    PersistentStateStore,
    StateDiffEngine,
    load_monitor_configs,
)


def test_title_indicator_parsing():
    assert IndicatorExtractor.parse_title_count("(3) Mail - Outlook") == 3
    assert IndicatorExtractor.parse_title_count("(14) Proton Mail") == 14
    assert IndicatorExtractor.parse_title_count("Mail - Outlook") is None
    assert IndicatorExtractor.parse_title_count("") is None


def test_dom_badge_value_parsing():
    assert IndicatorExtractor.parse_badge_value("5") == 5
    assert IndicatorExtractor.parse_badge_value("99+") == 99
    assert IndicatorExtractor.parse_badge_value("", aria_label="7 unread messages") == 7
    # Visible dot badge without digits defaults to 1
    assert IndicatorExtractor.parse_badge_value("", aria_label=None, element_visible=True) == 1


def test_protonmail_zero_knowledge_state_diffing():
    proton_cfg = AppMonitorConfig(
        name="ProtonMail",
        url="https://mail.proton.me/u/0/inbox",
        indicator_type="ping_only",
        zero_knowledge=True,
    )
    state = AppRuntimeState()

    # Boot at 0 unread -> establishes baseline, no alert
    assert StateDiffEngine.evaluate_transition(proton_cfg, state, current_count=0) is None
    assert state.initialized is True
    assert state.last_count == 0

    # State increases 0 -> 1 (even if preview_text was accidentally passed, it MUST be stripped!)
    ping = StateDiffEngine.evaluate_transition(
        proton_cfg,
        state,
        current_count=1,
        preview_text="Confidential Subject That Must Never Leak",
    )
    assert ping is not None
    assert ping["source_app"] == "ProtonMail"
    assert ping["message"] == "New secure message detected"
    assert ping["indicator_type"] == "ping_only"
    assert "Confidential" not in str(ping)

    # Steady at 1 -> 1 -> no duplicate alert
    assert StateDiffEngine.evaluate_transition(proton_cfg, state, current_count=1) is None

    # User reads message (1 -> 0) -> updates baseline, no alert
    assert StateDiffEngine.evaluate_transition(proton_cfg, state, current_count=0) is None
    assert state.last_count == 0

    # Another message arrives (0 -> 2) -> fires again
    ping2 = StateDiffEngine.evaluate_transition(proton_cfg, state, current_count=2)
    assert ping2 is not None
    assert ping2["raw_payload"]["delta"] == 2


def test_standard_app_preview_change_detection():
    teams_cfg = AppMonitorConfig(
        name="Microsoft Teams",
        url="https://teams.microsoft.com",
        indicator_type="dom_badge",
        zero_knowledge=False,
    )
    state = AppRuntimeState(last_count=1, last_preview_hash="abc12345", initialized=True)

    # Same count (1) but new message preview -> emits ping
    ping = StateDiffEngine.evaluate_transition(
        teams_cfg,
        state,
        current_count=1,
        preview_text="Alex mentioned you in #server-ops",
    )
    assert ping is not None
    assert "Alex mentioned you" in ping["title"]


def test_yaml_config_loading_and_state_persistence(tmp_path: Path):
    cfg_file = Path(__file__).with_name("apps_config.yaml")
    global_cfg, apps = load_monitor_configs(cfg_file)
    assert global_cfg["poll_interval_seconds"] == 30
    names = [a.name for a in apps]
    assert "ProtonMail" in names
    assert "Microsoft Teams" in names

    store_path = tmp_path / "state.json"
    store = PersistentStateStore(store_path)
    st = store.get("ProtonMail")
    st.last_count = 4
    st.initialized = True
    store.save()

    reloaded = PersistentStateStore(store_path)
    reloaded.load()
    assert reloaded.get("ProtonMail").last_count == 4
    assert reloaded.get("ProtonMail").initialized is True
