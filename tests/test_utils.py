from app.utils import choose


def test_choose_accepts_last_menu_option(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "5")
    assert choose("Select an action", ["0", "1", "2", "3", "4", "Exit"]) == 5
