from openipc_tftp.protocol import parse_client_filename
from openipc_tftp.sessions import InMemorySessionStore


def test_session_store_records_messages_and_env_values():
    store = InMemorySessionStore()

    first = store.record(parse_client_filename("id=cam123/"))
    second = store.record(
        parse_client_filename(
            "id=cam123/env/ipaddr=192.168.1.50/serial=abc123"
        )
    )

    assert first is second
    assert second.sequence == 2
    assert second.env == {"ipaddr": "192.168.1.50", "serial": "abc123"}


def test_session_store_records_var_and_set_results():
    store = InMemorySessionStore()

    session = store.record(
        parse_client_filename("id=cam123/var/bootcmd=run_boot")
    )
    session = store.record(
        parse_client_filename("id=cam123/set/bootdelay=ok")
    )

    assert session.observed_vars == {"bootcmd": "run_boot"}
    assert session.completed_sets == {"bootdelay": "ok"}


def test_session_store_records_reports_and_action_completions():
    store = InMemorySessionStore()

    session = store.record(
        parse_client_filename("id=cam123/report/filesize=1024")
    )
    session = store.record(
        parse_client_filename("id=cam123/run/smoke=ok")
    )

    assert session.reports == {"filesize": "1024"}
    assert session.completed_actions == {"smoke": "ok"}
