"""Initialize test Vaults without exposing recovery material in test output."""


def initialize_cli(run_cli, workspace, home, env_vars=None):
    result = run_cli(
        [
            "remote",
            "key",
            "initialize",
            "--workspace",
            str(workspace),
            "--recovery-output-fd",
            "1",
        ],
        workspace=workspace,
        home=home,
        env_vars=env_vars,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def unsupported_header(key, vault, sync_id, code):
    """Forge authenticated unsupported metadata only for rejection fixtures."""
    import json
    from aikito.workspace.encrypted_remote import (
        create_recovery_metadata,
        _metadata_mac,
    )
    from aikito.workspace.remote_wire import _canonical

    metadata = json.loads(create_recovery_metadata(key, vault, sync_id, code))
    metadata["format"] = "migrating"
    metadata["migration"] = {"epoch": 1, "owner_client_id": "1" * 32, "token": "2" * 32}
    metadata["metadata_mac"] = _metadata_mac(metadata, key)
    return _canonical(metadata)


def unchecked_header_resource(encoded):
    """Wrap malformed metadata so client validation, not fixture setup, rejects it."""
    import hashlib
    from aikito.workspace.payload import (
        FilePayload,
        ResourceDescriptor,
        payload_hash,
        encode_payload,
    )

    payload = FilePayload(encoded)
    return ResourceDescriptor(
        hashlib.sha256(encoded).hexdigest(),
        payload_hash(payload),
        len(encode_payload(payload)),
    ), payload
