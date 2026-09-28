"""Bundled template fingerprints used as the import comparison baseline.

A workspace file still matching any template that Aikito ever shipped is
treated as unmodified. When a bundled template changes, append its new
fingerprints here and never remove old ones; a test enforces that every
current template is listed. Markdown entries include LF and CRLF variants.
"""

from __future__ import annotations

TEMPLATE_HISTORY: dict[str, frozenset[str]] = {
    "agent:agy": frozenset(
        {
            "0dde0f81a02e2106757b30b25fdc6ac83fd8401f5835391937105d37c4eb5dc4",
            "2336508d79dcfc01e78936381ba2e816ccb1fcaa71061ee9aca517a06bc2d654",
            "275048211117676810baa4e1b51579b2bd469ccaf035deaefe5036b6e7f59b08",
            "aece47b24648f2822cb5787be1570599e655d1a506ae393f9691a239cea8f013",
        }
    ),
    "agent:claude-code": frozenset(
        {
            "25eee038667d050e648efd53a4996f887d97c7fbb0ce88913d041ff13063fe39",
            "73eecae61454a3c7287b20dc5f50678a73f65d80fe138852b11f02f68225247e",
            "d63457fe0f6f946b5bf3ae63536935b3cf836399a40fd6d85081704842f4a7f5",
        }
    ),
    "agent:codex": frozenset(
        {
            "1cc5ca571a702bde460d7f3c3a377e18d0a5d2b03c47380c12ebe25b426dd342",
            "8ccb59ad2fa0f430f80fb79e3b3d182a9e8deced0acd0c4e74e5e1ac0dda5801",
            "b3ba025ee766b48d0a1b10997d58de24e4f5c4712b4fa69787fa68f298d8ae10",
            "bb4de0b4d52f13f0b0a1025c028083561cd489524551995126d422b59cc3d4b7",
            "cf3a22508f3704174aad89c852d34d25e0426b1da2efe9c31cd3375c90d8e512",
        }
    ),
    "agent:dsh": frozenset(
        {
            "18f60ad83a516cf6e77000716be182bcf6c97f1e6176ad3b652ccb6af0cbb64b",
            "da4352d6f08c5aa97e5e41215d9122e4388d8945156653a2f564a0e483d46f7f",
        }
    ),
    "agent:github-copilot": frozenset(
        {
            "4b5edc25501b559f05f311b5a442d113c3cbebd80535c1b63ecc9e6863611114",
            "ebaa55297b4ecb5d6bed610af1f193340c528021a3aadee5d7d6057b74f88605",
            "fddba65a9756cafe34984f9b6e2c0d03b7361d1c64d789d5b6d7edf62280e2b8",
        }
    ),
    "agent:grok": frozenset(
        {
            "3507642b8e5648502fb315cf6f75c152b6047672daccb251b0acfd5f8cc7268b",
        }
    ),
    "agent:opencode": frozenset(
        {
            "1a1244ef498b633ddd0b5b120832a79affa4a5b99264f9b61e6d2073b71ae1b7",
            "3a7a6e69763080d88a30d7e334da797cb237dab1502fb8685e30621f4a7601fa",
            "56d5f99f327d609c7fefd40967850a9b649b3a457df5e7744b20111da926f868",
            "76fd84557eae2f99de7a2d0f189f49be908debd05e215ee2aa589d2516c3bf5a",
            "bd3334513a44d68de9fc27304e84303988af6d694fc29c6405aa749160f8efa4",
        }
    ),
    "agent:pi": frozenset(
        {
            "04048d1ffc19fc0fae4bf67be91bba49d5a6774329aa4d228fe7766fc3e9d419",
            "fdaf25e66dd2cee4e637962334d11860dbcd5b7634a56295c0d7def0018ac422",
        }
    ),
    "config:inbox.path": frozenset(
        {
            "44cb8de0fc3427e6267340f588d93fa83a9646158158eb43e5fc253d3aa4a6b5",
            "ee91b7ac12ba8f9d0d1f03c7609317c1d238e9d2cfea17e3a10e91bdd039e6e8",
        }
    ),
    "config:memory.stale_days": frozenset(
        {
            "624b60c58c9d8bfb6ff1886c2fd605d2adeb6ea4da576068201b6c6958ce93f4",
        }
    ),
    "config:update.check": frozenset(
        {
            "b5bea41b6c623f7c09f1bf24dcae58ebab3c0cdd90ad966bc43a45b44867e12b",
        }
    ),
    "global-instructions:AGENTS.md": frozenset(
        {
            "2584f98f5e16a45f58afa5973df5c9de2549a5d5db8329b887f32723b39ed852",
            "6958678f0f1e4b05e83ef8d207d63a9de71b99a16335978937757d8375ae3da0",
            "78b0f8c61bdb55f3201727fe5afcb1f9bca99cd3c32e655dddbf0d08aa093c99",
            "aa50eabe919c867d147c48c7f2aa5d99b773fec3e9e45ab3c3e2ed8e8fba0121",
            "b130074fb854151d46efb813ae7271de36f52c9293100ef3a2eec9611df3571f",
            "dc604372573331181135185eb0d4cebf1caccf547113ead628cee57575b36391",
        }
    ),
    "project-field:sync_mode": frozenset(
        {
            "6efec98683cc8a510eed3cef394bc29ac75d238b37525f77035541e142b7ee45",
        }
    ),
    "project-instructions": frozenset(
        {
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }
    ),
}


def template_fingerprints(resource_id: str) -> frozenset[str]:
    """Return the fingerprints counted as an unmodified template resource."""
    kind, _, name = resource_id.partition(":")
    if kind == "project-instructions":
        return TEMPLATE_HISTORY["project-instructions"]
    if kind == "project-field":
        return TEMPLATE_HISTORY.get(
            f"project-field:{name.partition('/')[2]}", frozenset()
        )
    return TEMPLATE_HISTORY.get(resource_id, frozenset())
