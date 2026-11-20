"""First-time setup for someone deploying their own copy."""
from typer.testing import CliRunner

from bankhub.cli import app


def test_init_invite_and_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("BANKHUB_DATA_DIR", str(tmp_path / "data"))
    run = CliRunner()
    r = run.invoke(app, ["init"])
    assert r.exit_code == 0 and "BEGIN PUBLIC KEY" in r.output
    data = tmp_path / "data"
    assert oct(data.stat().st_mode & 0o777) == "0o700"
    assert oct((data / "secrets" / "enable_banking.pem").stat().st_mode & 0o777) == "0o600"

    r = run.invoke(app, ["init", "--app-id", "00000000-0000-0000-0000-000000000000",
                         "--base-url", "https://example.ts.net/bank/"])
    assert r.exit_code == 0 and "Ready" in r.output
    env = (data / ".env").read_text()
    assert "PUBLIC_BASE_URL=https://example.ts.net/bank\n" in env
    assert "BANKHUB_REDIRECT_URL=https://example.ts.net/bank/callback" in env
    key_before = (data / "secrets" / "enable_banking.pem").read_bytes()
    run.invoke(app, ["init"])                                    # idempotent: the key is never replaced
    assert (data / "secrets" / "enable_banking.pem").read_bytes() == key_before

    r = run.invoke(app, ["invite", "--label", "phone"])
    assert r.exit_code == 0 and r.output.startswith("https://example.ts.net/bank/#invite=")

    from bankhub.web.server import load_web_config
    assert load_web_config().cookie_path == "/bank"             # derived from the address

    from bankhub.web.auth import AuthStore
    code = r.output.split("#invite=")[1].split()[0]
    AuthStore(data / "web.db").redeem(code)
    assert "phone" in run.invoke(app, ["devices"]).output
    assert run.invoke(app, ["devices", "--revoke", "1"]).output.strip() == "Done."
    assert "revoked" in run.invoke(app, ["devices"]).output


def test_demo_data_is_plausible_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("BANKHUB_DATA_DIR", str(tmp_path / "demo"))
    r = CliRunner().invoke(app, ["demo", "--months", "6"])
    assert r.exit_code == 0, r.output
    import sqlite3
    from bankhub import queries
    c = sqlite3.connect(tmp_path / "demo" / "bankhub.db")
    c.row_factory = sqlite3.Row
    assert c.execute("SELECT count(*) FROM recurring_series WHERE direction='in'").fetchone()[0] == 1   # salary
    assert c.execute("SELECT count(*) FROM transactions_clean WHERE category='uncategorized' AND amount=-3500").fetchone()[0] == 0
    names = " ".join(r[0] for r in c.execute("SELECT DISTINCT merchant FROM transactions_clean"))
    assert "Ana Example" not in names and "Mihai Example" not in names
    # Refuses to mix with real accounts.
    c.execute("UPDATE accounts SET uid='real-1' WHERE uid='demo-salary'")
    c.commit()
    assert CliRunner().invoke(app, ["demo"]).exit_code != 0
