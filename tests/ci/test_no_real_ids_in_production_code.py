"""Production code carries no real provider ids and no workstation-specific paths.

A data-only refresh must need no code change, so an account, product, order or
transaction id (or a path under one person's profile) may never be typed into
production code: it is derived from admitted data or configuration instead.

Two kinds of check, neither of which contains an id:

* structural: shapes that only a real id or a user-profile path can have;
* hashed: every id-shaped token found in production code is hashed and refused
  when it is one of the ids that were known to be real when this guard was
  written (SHA-256 of the token text; the ids themselves are not stored here).

Data pins (contracts, acceptance baselines, retained release inputs, schemas)
and archived scripts are data, not production code, and are not scanned here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CODE_SUFFIXES = (".py", ".js", ".mjs", ".ts", ".sh", ".ps1", ".yml", ".yaml", ".toml")
NOT_PRODUCTION_PREFIXES = (
    "tests/",
    "docs/",
    "contracts/",
    "generated/",
    "schemas/",
    "provenance/",
    "acceptance/baselines/",
    "acceptance/expected-baseline.json",
    "ops/preserved-scripts/",
    "ops/mvp2-release/inputs/",
)
# Opaque ids: account/transaction ids minted by provenance and the provider
# ledger, long provider numeric ids, and 16-hex labels.
ID_TOKEN = re.compile(
    r"account-[0-9a-f]{16}|txn-[0-9a-f]{24}|(?<![\w.-])\d{15,}(?![\w.])"
    r"|(?<![\w])[0-9a-f]{16}(?![\w])"
)
STRUCTURAL = {
    "account id": re.compile(r"account-[0-9a-f]{16}"),
    "transaction id": re.compile(r"txn-[0-9a-f]{24}"),
    "user profile path": re.compile(
        r"(?i)[a-z]:[\\/]+users[\\/]+(?!public\b|default\b)\w"
    ),
}
KNOWN_REAL_ID_SHA256 = frozenset(
    {
        "0fbbeda65801b5d8d9747618fbd8ff841edb568eee4706b72e58018e9722ff48",
        "0fce6710979fcb92de5afcf02694aa6ba862c6b4d58a530669b524b59e427979",
        "15dda412db1b235449a1adad6d7377b3dbac9f027354db34844bfa3460df23fa",
        "18a27b3f61fe2a87151b90c04aef187b7b1705ac615af0c10d781ff708956409",
        "18d287b02de4b9fc719ea95abb245d25a64c03915b773e56da3e01a5680d9d42",
        "19d9971fa367376a91ccd9b661b1946459a31918d836e58cadb7e7a0c71c143e",
        "1d17e30b635152eae27ea3b2c82ddb644a353c6c202fc0fc5c08bab87a52b457",
        "1ec13f34dcf7ac65a25bccfeb50f869c49762ed20b86e682a10a7d3293485be8",
        "225ec5540b3bdbcd65b371275534840a0c4728f22363be012cc73ffd7efcc985",
        "28bd148ae13635791b84bf48c139f4c1553fbdbeab04b5bbb9ec70c83b1c3e41",
        "28d62458d5ec71bb9fbbb33e727dc5fa3db8d9c3500ef09ec237a2fff7cca054",
        "2c7821c6852bc5bd8c3b004cc3352242352fe06db2c585b4e6a9163c2c83f65e",
        "32ebbad52f9afc7ec14368e3f47f5e20540314f661c141d2b99b0d5940571606",
        "343da3c3d560a9e5413a22532882d40a6e7f56f24952e71020f2817dae4e6592",
        "3483593d9b19a13d3efc4e303d235ac22badc979f10f8fa9ea1078ab2d0e896c",
        "3821f042e4a4b7930326818869c7de2288c0b9ac39e378a8f7e27a2c2cac6e81",
        "38ad6df3e628f4c2560b56c3020b6110be5d4b8e3b99a2808817a4d29018615e",
        "41d48ea83c243ac2311da1de795f25638967888c2dabbeec9eb0a2d3d94e1055",
        "41d91db95b99d2fee9fc1898030a88edd05f5a73f775ad28a9112da389d27fea",
        "41da781779db94e462f0791a5742c65914eb1cfc659a55da0050150ac29b37a0",
        "4208cbd3b9e5f127c956f896b5e49889b4655534901b2937c4c7a8cec8473b16",
        "420c1fe33513e6bc6bd2a270cdec00bf7b19a09ae4450b1c4fa1eeaeb8bce95a",
        "44b1b10ddfe7f07ba5573abacc1a8f21e628c2146583dab973fbbb3ec08adbe1",
        "4540df34d0b8f1ddd3268079341dee2988d4f07a70e168410c7ff722b15b0169",
        "4a9ea73fc9a08ee2ad87b92b424cb663bb49a1b9d551122fedc7aa0c8c9a76c9",
        "4aa647bae0995b2ff6f5c5d64d48179ed8376aee0e18d0a2b793fe977e6048b3",
        "4c39b56bea9daa9539828a3c24ec690cdc44eb1b72c11505f59af3c22c62622f",
        "4e46f762e25313be7ca33f6957b9a65548b0fc3d23ba1c574eb7baef5abf9e4c",
        "5030c776fe6f4fef6b8741bef8b27eb2550fedf4a3cc179e14e6ed70bcec1c9f",
        "50a78ceea1f20b067a4c205915e0cc55ce8be8461214af4886d656d5bb0c062f",
        "51784ae913b58346ce355c2ec16c17c9c36b987265f6b7b3f33b4263587c7759",
        "63fe4f2c3d413a37f2bce815ab85b1fe72685c8215fb3df7ba8fa0288e223e90",
        "643e9e487ea3ade2621e045b66c771188343e9a8bd9ef9b571602ad9ecdb3798",
        "65dc44d603c8da2fd6e59d28395b534b39f34915c9a980c15a2508ee29d21e66",
        "67ffab05e2e951d2f9d8658e5221c8b05939cc6394bfb3e50e94c76bdbccbb86",
        "6cff0722958400678d4d2f3a75335d2d041097c0d6aeccac8f1067110bb61dab",
        "6d9e6c02aeb920b5b687befb0bff1cf8986202ffe60a237c69c5e74094e60ec4",
        "77fb9a3cd56736301ff0db25086dd0f267d9e27a9863997472c2ab1e3402b114",
        "7896fb758dfbda960980ca554bcb8d4ba547f675582718261f5c6d4e17a41fcf",
        "7ae39f01e62fe1e000a1ca2fce88ff948661e5731779326f70b19d898d7571ae",
        "81af93a818e0c34bd26e37f5f27cd507af95d8b8f193a40dc76d83f69b8b08eb",
        "875def3024ae5a3a22c35387e0422733206f81d2c64e596da9219e5640187ca2",
        "89d42a94f167de83a69a578bfd48b0a6c7da65ebb4b16810936e58d41688a82d",
        "8b09a2ffcf83aa1521b1493e59659449f3985c6f76995087530838d18658e137",
        "8c4aeceeeee8071fe94917a1965313a0b29ac4ad560b740fc0690c9a970f6128",
        "8d4bbbc5240a6fb90d3586b2656a7ff7695240206bbb8b8ee3b9d947ea8e2e0e",
        "8f768971eb59b4b2250b031df6cce9ec8d859a81dd8db1f6153fee14cc8eaeca",
        "9004d6bc00f1457c97769ab1728104a9e323922202d313298b62d5606446cea5",
        "91067d74937a960d4128bc087bdf033d072b9a7420650559c481f31e364295a5",
        "9338182e0582dca5cde25296615d59c4de1babb1b9505dc907157f484c1ef473",
        "9a91a283eb4b12858e3bacef93666a6de4babad967d64ebf818cbbfa00cc30a0",
        "9fb1e105f245f1686429ef596a98907eeb162b3fc18f68b85ba2ad2f90467900",
        "9fe95d88c5f7f4d67c2c3738b680a594acc9be2b2a6bbd4245b9c54a391257c1",
        "a10bda4aecbf6ddd6c0e133bc4a9836a7fcfd4b6894fa43e972b3a4aa9d0007f",
        "a2208130652bd5d4cb88f2eb57db13768ddbbce476a22b035bae56b6516acef0",
        "a24c1a0207a1635b97a7657ee589721d1b73999b85d2b34aa770e6bd0661fd1a",
        "a2be527b3eefb4a7f296fc1c7ba8d774068cf780ab2146b50c3038a2d07bc749",
        "ad19b626ab1eff8db8bc9c553aa486652dcd0e7d8ee115755e9d1eea37a6da6b",
        "ae3c322aa5e9a3da891293822e2225dfdc953fddc85b3da63da56842a73bee62",
        "ae8640f895e15c31c2fabfd36bd852f368e21da1df3551df5fcc4892c89b5ba0",
        "b2652adc0eeecad74919ff42016423c77e1ccf1de34706c4a36b357762c2138c",
        "b67ffe7ee4dbe9c53064835dce6db7545a2eac6944bc7d9fae2816530a8b7429",
        "b82900887b77e63d1bd00b8f3a409d34e41bd4b7e490e50c82592d45eff40a4a",
        "b82d7eed78f51353165ce2ecf4c3a6fcf61b3363e0fd3cc2ad5bf562c339acba",
        "bc2f4229f220615031239c2862cf794dd7ddf97d907e64387699fe3dcfe7f033",
        "bf7b72151d2edcd72b72af75d396036dcf432584570d8cfac2f09b0bd1415fd5",
        "c1ca3b5b069a618fb93511889af769bf8caf79fe608c06b258c689c2a4cca258",
        "c54e99749fe9a6a2f591052c433cd2a12a22b5244837ad6088610160178914c0",
        "c93f542b08338bdf3b8eeb91004fed8a1c2b1286996221183098d646ce74b34a",
        "ca7692294ad9b39fa75a800f718dfc352afed721010cd20c23fe06d6bf1fe2ae",
        "cc662214ebd22ededf2e7481a1ef56b92f92d166d8907d9efdf1fb72098d2947",
        "cdacfd04c77b74dacf913c13965bbdd390c43eb49dd58584f13c74a0a8245037",
        "cf11fae3786bc96b1557ff5c6b3208684b6e999ab065ce0bd73d65cca6b2e731",
        "d46c2c6b50b174330024d8d3df4fada936a3a702bab3e949307e2be602e047db",
        "d602a93cf552d5d18e5b372104a2e92c1bbdfc979dd0bb47b5b171cf05eaa942",
        "d750956738c3691350632e3acf8b95232dd021173f204dde0463e63efb892742",
        "d8b4f3a982708d8e185a94b7ede14a9f591dc3c3ba3f7f6ea10861d3854b2596",
        "dac2bfabf9a417399aacf488469f20a6aa6f6c5cd4c087afcb23a1f9c2ac4e4a",
        "dcb9b87f836df2caef7ea4fbc376e0ead18daa314dfd0d0f3609b26e6f2393f1",
        "dce65b20ff4ffbdbdb66cbef90345151bf7d151ca5466e31e5de02dbae03f6ac",
        "e2ddebf7ca81343dec645115cd9eb74585b13b5d3f50fa3b4e3bf0fe2defeaec",
        "e436ea8e64cd519ebdfe7650e3b6514bff3ff5eec742be54b0fd640fb33bd1f4",
        "eddf415f633a7641984a39cd06c66a8a02042eaa33c8f6f9fcaf855c41e3cf7d",
        "ee0302c6f2b6ad91cde22bb91ce9f1ca89bad745a951690f88898974747faccc",
        "f3851a649c0045f59f640fe25e4cb05e39392856ded2cf5c1515d3bf9bddd2f5",
        "f6e79c1f2ae23f4c6eea2af4b1ec75357410ba93b59c452cae36115e0658089d",
        "f735d015d8941f66d4391eeb0f7d82e7b03e0686416202e501c10ffabfc13962",
        "fb2cf5ae3b2d22b16f1b72117a879eef073df0944694d81422ddf2e42c8722d9",
    }
)
# (path, sha256 of token): a hashed non-managed TWS account label that an
# owner ruling excludes from the IBKR read boundary.  The owner declares it per
# generation, so it can only move into the IBKR spec together with the frozen
# R2.1 spec; until then it is the one exempt literal.
EXEMPT = frozenset(
    {
        (
            "packages/ibkr/src/quant_engine/ibkr/generation.py",
            "15dda412db1b235449a1adad6d7377b3dbac9f027354db34844bfa3460df23fa",
        ),
    }
)


def production_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [
        REPOSITORY_ROOT / name
        for name in listed.split("\x00")
        if name.endswith(CODE_SUFFIXES) and not name.startswith(NOT_PRODUCTION_PREFIXES)
    ]


def _relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_the_scan_covers_production_code() -> None:
    names = {_relative(path) for path in production_files()}
    assert "tools/provider_ledger.py" in names
    assert not any(name.startswith(NOT_PRODUCTION_PREFIXES) for name in names)


def test_no_structural_real_id_or_user_path_in_production_code() -> None:
    found: list[str] = []
    for path in production_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for kind, pattern in STRUCTURAL.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{_relative(path)}:{line}: {kind}")
    assert not found, "real-id shaped literal in production code:\n" + "\n".join(found)


def test_no_known_real_id_in_production_code() -> None:
    found: list[str] = []
    for path in production_files():
        relative = _relative(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ID_TOKEN.finditer(text):
            token = match.group(0)
            digests = {_hash(token), _hash(token.split("-", 1)[-1])}
            if (
                digests & KNOWN_REAL_ID_SHA256
                and (relative, _hash(token)) not in EXEMPT
            ):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{relative}:{line}")
    assert not found, "known real id in production code:\n" + "\n".join(found)


def test_the_guard_detects_what_it_guards_against() -> None:
    sample = "account-" + "0" * 15 + "1"
    assert STRUCTURAL["account id"].search(sample)
    assert STRUCTURAL["user profile path"].search("C:/Users/" + "someone/x")
    assert not STRUCTURAL["user profile path"].search("C:/Users/Public/x")
    assert ID_TOKEN.search("order " + "1" * 19)
    assert not KNOWN_REAL_ID_SHA256 & {_hash(sample), _hash("1" * 19)}
