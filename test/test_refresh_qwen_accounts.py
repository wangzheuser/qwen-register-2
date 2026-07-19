import json

from refresh_qwen_accounts import apply_results


def test_apply_results_refreshes_keeps_uncertain_and_deletes_dead(tmp_path):
    accounts_file = tmp_path / "qwen_accounts.json"
    txt_file = tmp_path / "qwen_accounts.txt"
    accounts_file.write_text(
        json.dumps(
            [
                {"email": "live@example.com", "password": "p", "token": "old"},
                {"email": "dead@example.com", "password": "p", "token": "old"},
                {"email": "unknown@example.com", "password": "p", "token": "old"},
            ]
        ),
        encoding="utf-8",
    )
    txt_file.write_text("old\n", encoding="utf-8")
    results = [
        {"email": "live@example.com", "state": "live", "token": "new", "name": "Live", "role": "user"},
        {"email": "dead@example.com", "state": "dead"},
        {"email": "unknown@example.com", "state": "uncertain"},
    ]

    deleted, refreshed, backup = apply_results(accounts_file, txt_file, results, tmp_path / "logs")

    accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
    assert (deleted, refreshed) == (1, 1)
    assert [account["email"] for account in accounts] == ["live@example.com", "unknown@example.com"]
    assert accounts[0]["token"] == accounts[0]["active_token"] == "new"
    assert accounts[0]["qwen2api_sync"]["status"] == "pending"
    assert backup.joinpath("qwen_accounts.json").exists()
