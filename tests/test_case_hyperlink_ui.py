from pathlib import Path


def app_text() -> str:
    return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def test_case_link_opens_standalone_detail_in_new_tab():
    app = app_text()
    assert '"lw_detail_page": "1"' in app
    assert "target='_blank' rel='noopener noreferrer'" in app
    assert "def _render_linked_detail_page" in app
    assert '_detail_dialog_body(item, cfg, standalone=True)' in app


def test_detail_page_is_rendered_before_dashboard_tabs():
    app = app_text()
    detail_guard = app.index('if _query_param("lw_detail_page") == "1":', app.index('cfg = load_ui_config()'))
    tabs = app.index("tab_dashboard, tab_profile")
    assert detail_guard < tabs
    assert "st.stop()" in app[detail_guard:tabs]


def test_case_link_uses_stable_identifiers_not_only_row_index():
    app = app_text()
    assert '"lw_detail_case"' in app
    assert '"lw_detail_item"' in app
    assert '"lw_detail_auction"' in app
    assert '"lw_detail_profile"' in app
    assert 'lw_detail_index={row_index}' not in app


def test_new_tab_resolves_detail_from_database_history():
    app = app_text()
    resolver_start = app.index("def _resolve_linked_detail_item")
    resolver_end = app.index("\n\ndef _dispatch_linked_detail", resolver_start)
    resolver = app[resolver_start:resolver_end]
    assert "_history_detail_items(cfg, limit=5000)" in resolver
    assert "_find_detail_item(history_items, wanted)" in resolver


def test_result_table_forces_visible_horizontal_and_vertical_scrollbars():
    app = app_text()
    assert "overflow-x:scroll" in app
    assert "overflow-y:scroll" in app
    assert "scrollbar-gutter:stable both-edges" in app
    assert ".lw-table-wrap::-webkit-scrollbar{width:14px;height:14px;}" in app
    assert "min-width:2100px" in app
