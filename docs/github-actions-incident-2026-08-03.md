# GitHub Actions pre-run incident — 2026-08-03

Issue: [GOV-INC-001 #194](https://github.com/dachent/quant_engine/issues/194)

## Disposition

PRs #162 through #193 were merged while every required workflow was
unsuccessful before runner allocation. This was not a project-code or test
failure: the inspected jobs had `runner_id = 0`, no runner name, and zero
steps. GitHub attached this annotation to the affected checks:

> The job was not started because recent account payments have failed or your
> spending limit needs to be increased. Please check the Billing & plans
> section in your settings.

Repository Actions remain enabled. Actions are restricted to the selected,
SHA-pinned allowlist, and the default workflow permission is read-only. The
failure is therefore an account-level pre-run allocation/billing condition,
not disabled repository Actions or a failed workflow step.

The repository stays private on GitHub Free. This incident does not authorize
a purchase, plan or visibility change, spending-limit or payment-method change,
self-hosted runner, weakened check, or classification of local evidence as
hosted success. The accepted local evidence remains useful diagnostic and
recovery evidence only.

Issues #152 through #156 have been reopened and blocked by #194. Previously
accepted provider/data pointers and releases were not changed by that issue
state correction.

## Inventory method and invariant

The inventory was acquired read-only from GitHub's Pull Requests, Actions runs,
Actions jobs, and check-run annotations APIs on 2026-08-03. All 132 listed
workflow runs were independently re-read with zero exceptions. For every listed
PR head, exactly one run was found for each required workflow. The normal job
counts were Data boundary 1, Issue guard 2, Quality 6, and Security 3. Every
listed job had `runner_id = 0` and zero steps. PR #169's Data boundary, Quality,
and Security runs concluded `cancelled`; all other listed conclusions were
`failure`. A separate main-push set for the final merge SHA had the same
zero-runner, zero-step condition.

`Local disposition` for every row is
`PRESERVED_NOT_A_HOSTED_ACCEPTANCE_SUBSTITUTE`.

| PR | Work label | Exact head SHA | Merge SHA | Data boundary | Issue guard | Quality | Security |
|---:|---|---|---|---:|---:|---:|---:|
| 162 | PROVIDER-LEDGER-001 | `b15f9e21021cf3c94ef93691976c288c099cdae1` | `602fddafefc1533de58c8be5388dbfd33b35a6bd` | 30773824964 | 30773824981 | 30773824950 | 30773824959 |
| 163 | DATA-PIPE-001 | `f08759d94213af8090b89407e8bd7cb59a9f9377` | `27df77fb6b1f5f2344b00c4b24195f7898a748a5` | 30774371847 | 30774371843 | 30774371867 | 30774371863 |
| 164 | DATA-PIPE-001 path fix | `70fc1e25b24eb3dae407e0773dbbc0c6d0231b7f` | `c7fb2b8846fe189ddbe1311822ab8900b72d3d7b` | 30774555716 | 30774555718 | 30774555741 | 30774555721 |
| 165 | DATA-DR-001 | `4fabf52bbee924b1be4ea39a62498198da1fa5ea` | `c51d99446e5eb59516aeb2f00ca6890e92199218` | 30775013311 | 30775013324 | 30775013320 | 30775013307 |
| 166 | Review blockers | `4b1ab03e262ec4cfbe8a67d7e41fd7fe64b49b6e` | `8e1f42c477c6bb34f64b4cfff4352f9f9184d5a2` | 30776395020 | 30776395007 | 30776394990 | 30776394996 |
| 167 | Exact freshness generation | `8e6d8e4a6c430fd842d4de7369a6fc6665ec43d0` | `015b76b8299e32de500d8756161a380da8f81592` | 30776662859 | 30776662858 | 30776662862 | 30776662869 |
| 168 | HWM freshness copy | `38f2e5c377a42a4611da6b0b887c33b958a943a3` | `7a2dea2a7fda1594c04ad4c9c1e966019aec424b` | 30776815095 | 30776815166 | 30776815164 | 30776815122 |
| 169 | HWM freshness UI | `968e8bf6b56db469c017ab05ac3e292f7dca75a2` | `76540fb8c2e534885f49ae5cafe294e51e1c34e6` | 30776952650 | 30776952615 | 30776952619 | 30776952640 |
| 170 | Rehearsal path fix | `01d50af11eaa223588b47bce53dbb1bb9fc97165` | `b209ee74a53b782ccb71c518d4b257c0672f0116` | 30777415211 | 30777415148 | 30777415103 | 30777415065 |
| 171 | Stage rename denial | `0a2cf534d1e05775718658d196eaca4728410ba2` | `cf29d4b072aa5a0d51a5c98ee336368ac872ab9e` | 30777549158 | 30777549125 | 30777549124 | 30777549121 |
| 172 | Strict types | `1ccc1f51c5948b83baef5113306f2031f5210e4f` | `cba2cb5a8722d6860545184ef428bebe1fea7c57` | 30777770426 | 30777770433 | 30777770467 | 30777770454 |
| 173 | Terminal recovery locks | `b74be08e82906c4c7978434f50afbf44b794ac76` | `e000719e7201345b6464983209acc910054e2685` | 30778625664 | 30778625730 | 30778625684 | 30778625680 |
| 174 | Publication freshness | `d79062eec8fe7c54b54c862e0f1c16a42b26e8b6` | `df32af85a4d30023d39c130cc0153138bcc93033` | 30779013160 | 30779013165 | 30779013168 | 30779013142 |
| 175 | Freshness metadata | `f688d0f3e6ecaaf626ad531ca6fe8e228c0707e9` | `3837dc205e609d0fec13c317ad6cd09711b9d617` | 30779132168 | 30779132150 | 30779132153 | 30779132119 |
| 176 | Dynamic HWM copy | `0fed9c30ec7199f5672e354b432a2d4bb8cb899f` | `6d6cc849b868c02b749d452ece1b2d946352c428` | 30779344743 | 30779344742 | 30779344749 | 30779344762 |
| 177 | Panel operational date | `e5b600202e2a296b0e2c8046f54dacde3014f480` | `41a8dd06bed96f8a124b7a8657eba906c5ee75c4` | 30779742133 | 30779742180 | 30779742187 | 30779742160 |
| 178 | Recovery formatting | `dc18aef74e91ce3976451ce3916857331492b22f` | `10cc4041635506f355d2f8399cd63c634f07adda` | 30780021964 | 30780021935 | 30780021956 | 30780021918 |
| 179 | Panel lineage and launcher | `52a50bc149dde1d7bb01af6d2b3b77e3d4d527ea` | `e278840056079393ee50bc90925fedef480604d6` | 30781287694 | 30781287726 | 30781287697 | 30781287701 |
| 180 | Frozen source contract | `7cfe0e5d3837d30c6b78ac757780e2eac1521f4c` | `e6aaa542b17b1260b357ae34d4189657e0e740a0` | 30781448135 | 30781448136 | 30781448130 | 30781448115 |
| 181 | Launcher hash preservation | `09325ae9d78bf4d4efec22739f695fa28efef1eb` | `93e346de47a2ca1eaf9f8bda3139f35a29469958` | 30781931036 | 30781931087 | 30781931054 | 30781931058 |
| 182 | HWM qualification | `3d0549079c46a001207913eb14523b00f7e25939` | `0223890ee85e62cbb6bff68b75d5d5a579b7ae48` | 30783739974 | 30783739939 | 30783739978 | 30783740001 |
| 183 | HWM state label | `913ad2ba14bb2cdd8f94704fdc4afd41909559cf` | `92c6c29421b388ab3e8ff7fb9f1c0cdf908f5528` | 30783924833 | 30783924865 | 30783924854 | 30783924848 |
| 184 | Snapshot toast contract | `934aedc732fe235e60d110838322279b151c3f63` | `5ef6af2a4ce773b168041eca2fd9e1711a944bdf` | 30784161969 | 30784161975 | 30784161967 | 30784161972 |
| 185 | Governed HWM labels | `6712fdea9e5f4f193f22439d885582ef2273fe25` | `3e7efa4cdb39e880a9e094bdebb3a0ccbbb9372c` | 30784384949 | 30784384950 | 30784384942 | 30784384919 |
| 186 | Mobile provider evidence | `c09e6ec302a706d5e0f919cda89656b7bc6b7479` | `8b51782a2323899c72c55bfa3cdda9413dcac9c7` | 30785910624 | 30785910611 | 30785910659 | 30785910606 |
| 187 | Canonical strategy rollforward | `6d26db29207fb6a5082660e184c4b80b8ca08a69` | `04552daa07bfd726125e9be234f421c73c9844f8` | 30789465700 | 30789465711 | 30789465718 | 30789465728 |
| 188 | Provider limitations report | `3d1a17c2dc88b891e653b7d25616f7a249b5bd07` | `b7de50ff5fe3688ffa0f5f67a23d7e666a875fb5` | 30789839575 | 30789839527 | 30789839385 | 30789839380 |
| 189 | Data review v7 | `d3a33d95a50ee771366df3f66205627d48811849` | `7858a55ab6f15eaa6c0dae27a618e12d954e7c14` | 30794640863 | 30794640854 | 30794640908 | 30794640902 |
| 190 | Correlation digest | `92ba936738ad35dca8461e7c32dfd1b46f567e56` | `c117e3036bf30b5e8a22bed1c86aac408251b644` | 30796231640 | 30796231643 | 30796231228 | 30796231658 |
| 191 | Release v7 | `90c4018dbc96ef932cf6ee57f409d109291b3449` | `d5ee80d12254015ff25285fd1ddeb8cbffd59685` | 30797557887 | 30797557807 | 30797557771 | 30797558026 |
| 192 | Release v7 final | `e8f0b432476f94175cabb5c2ee93c771703b8ca3` | `1da5054de0e7b8252143ff0d1b0debed71bddc20` | 30799352119 | 30799352082 | 30799352154 | 30799352108 |
| 193 | Release v7 acceptance | `78d2aa11c43f790dad6dcb85fed347e88542719c` | `49de974cdc0b70c6e08b2e0788082984710bd2cb` | 30800054790 | 30800054734 | 30800054760 | 30800054880 |

Final main SHA: `49de974cdc0b70c6e08b2e0788082984710bd2cb`.
Its required push runs were Data boundary `30800060990`, Issue guard
`30800061078`, Quality `30800060832`, and Security `30800060816`; all concluded
`failure` before runner allocation with zero steps.

## Recovery gate

No further repository change may be merged under the current operating canon
until all four required workflows complete successfully for one unchanged
candidate SHA, or the user separately approves a canon amendment that names
the replacement proof and its risks. A GitHub account-state correction may be
investigated read-only, but changing billing, spending, payment, plan,
visibility, or runner configuration remains a stop-and-reapproval boundary.
