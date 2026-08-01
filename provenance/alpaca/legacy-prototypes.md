# Alpaca legacy prototype disposition

The eight files below are the complete code-side inventory remaining after the
security quarantine. A value-free static review found no nonempty literal API
credential. Their exact bytes are retained as immutable local source-run
evidence outside Git because several prototypes expose broad raw HTTP behavior
and all were superseded rather than made executable in the monorepo.

The repository captures their supported read capabilities in
`quant_engine.alpaca.AlpacaReadClient`; unsupported or unsafe behavior is not
carried forward. Hashes below bind every legacy code artifact to its local
provenance record.

| Artifact | Bytes | SHA-256 | Disposition |
|---|---:|---|---|
| `alpaca_export_final.py` | 14,285 | `b8d1411e66fec60bef0852100b5e44807eb62510533b7ae44984b618e2cb7f73` | Superseded; generic raw HTTP surface not imported |
| `alpaca_export.py` | 14,693 | `dfb716098809ffaaf4923e29b67a4456db1496405080927bdf85e00384622131` | Read capabilities consolidated |
| `export_all_alpaca.py` | 12,405 | `5f186c1e83a220cd78e77724175c12dc00bc833bd3c4e43bde31226188dc96b2` | Read capabilities consolidated |
| `get_all_activities.py` | 13,724 | `d582dd5245d02f934955153d02f970be27544ae186671b9b2d1ce943dd020dd7` | Bounded activities route consolidated |
| `minimal_export.py` | 13,475 | `78181f62e3065005a57135400a32d7c8a7387def3a016b51c67067b9be665b89` | Read capabilities consolidated |
| `requirements.txt` | 65 | `148a6645e15b7edbeea525fbc2e1ba88b5dce334fe3e5c08bfd175ebe742dae5` | Dependency reference only |
| `simple_export.py` | 13,656 | `3bdeb793f4aff70e6757a1349b98618ba2517cc8b214bf43a57b4e4f9a50ea4d` | Read capabilities consolidated |
| `test_auth.py` | 1,771 | `8a8b6a444687bd5203432475d7dd6d364a8b2628bc0ed5099dd2ccc216508bd3` | Superseded by offline fake-transport tests |

Plaintext reports are governed by `ALP-SEC-001` and are not part of this code
inventory. No real data, external account identifier, or credential is stored
in this repository.
