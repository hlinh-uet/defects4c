# `sqlite___sqlite` - Defects4C x Unified-Debugging

## 1. Muc tieu

Thu muc nay chua pipeline sinh metadata Unified-Debugging cho project
`sqlite/sqlite` trong Defects4C. Flow duoc can chinh theo `php___php-src`:
tach Phase A lay outcome va Phase B lay GCOV coverage, nhung SQLite khong dung
ASAN.

Input chinh:

| File | Vai tro |
|---|---|
| `bugs_list_new.json` | Danh sach 6 bug, gom `commit_before`, `commit_after`, `files.src`, `files.test`, `type.id`. |
| `project.json` | Khai bao repo va template build/test Defects4C goc. |
| `build_tpl.jinja` | Template build goc, dung `./configure` + `make`. |
| `test_tpl.jinja` | Template test goc, chay `./testfixture <test-file>`. |
| `build_meta_sqlite.py` | Script sinh `*_meta.json` cho Unified-Debugging. |

Output chinh:

| Output | Y nghia |
|---|---|
| `/out/unified_debugging/sqlite/metadata/{bug_id}_meta.json` | Metadata de Unified-Debugging doc. |
| `/out/unified_debugging/sqlite/raw/{bug_id}_meta.json` | Raw output, hien cung noi dung voi metadata. |

Tren host, `/out/...` tuong ung voi `defects4c/out_tmp_dirs/...`.

## 2. Luong xu ly cua `build_meta_sqlite.py`

Voi moi bug trong `bugs_list_new.json`:

1. Tim repo tai `/out/sqlite___sqlite/git_repo_dir_<bug_id>`.
2. Checkout fixed tree bang `commit_after`.
3. Tao buggy tree theo kieu Defects4C: giu fixed tree va chi overlay `files.src` tu `commit_before`.
4. Build SQLite bang `./configure --enable-shared=yes --enable-static=no`, `make`, va `make testfixture`.
5. Chon test:
   - luon include `files.test` cua bug;
   - them toi da `--max-tests` test random tu `test/*.test`;
   - mac dinh `--max-tests 50`;
   - `--max-tests 0` nghia la them toan bo selectable tests;
   - random seed mac dinh la `20260428`;
   - loai cac wrapper/heavy/noisy tests nhu `all.test`, `full.test`, `quick.test`, `soak.test`, `malloc*`, `*ioerr*`, `*fault*`, `crash*`, `speed*`.
6. Phase A:
   - chay buggy tree de lay `tests[*].outcome`;
   - neu bat `--dual-run`, checkout/build fixed tree va chay cung danh sach test de lay `outcome_fixed`;
   - luu toan bo test da chay, gom PASS, related-fail va unrelated-fail.
7. Phase B:
   - checkout lai buggy tree;
   - build voi GCC coverage flags;
   - coverage link flags force export `__gcov_dump` de signal handler co the flush `.gcda`;
   - chay lai cung danh sach test da chon, khong loc theo outcome, va parse `gcov`;
   - khi test segfault/abort/bus/ill/fpe, script preload handler goi `__gcov_dump()` best-effort truoc khi process chet;
   - coverage luu dang `"<file.c>:<func>"`.
8. Ghi metadata:
   - `bug_id` lay tu `type.id`;
   - neu `type.id` trung nhau, file output co suffix `__<commit_after[:12]>`;
   - `raw/` va `metadata/` co cung noi dung;
   - JSON duoc ghi atomic de tranh file rong khi Ctrl-C.

Luu y: SQLite test tao temp DB/files trong working tree, nen script chay test tuan tu.
Mac dinh khong dung full SQLite suite vi project co hon 1000 file `*.test`; neu can full selectable suite thi dung `--max-tests 0`.

## 3. Docker + Run Flow

Project nay dung image rieng `defects4c/Dockerfile.sqlite___sqlite`.

Chay tu thu muc `defects4c`:

```bash
docker build -f Dockerfile.sqlite___sqlite -t defects4c-sqlite-sqlite:latest .
```

```bash
docker rm -f my_defects4c_sqlite___sqlite 2>/dev/null || true
docker run -d --name my_defects4c_sqlite___sqlite \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  defects4c-sqlite-sqlite:latest sleep infinity
```

Mount / output thong nhat:

| Mount / option | Y nghia |
|---|---|
| `$(pwd)/defectsc_tpl:/src` | Source Defects4C template trong container. |
| `$(pwd)/out_tmp_dirs:/out` | Repo da clone, raw/metadata output. |
| `$(pwd)/patche_dirs:/patches` | Noi chua patch neu can validate APR. |
| `$(pwd)/../Unified-Debugging:/udbg` | Code Unified-Debugging. |
| `--metadata-dir /out/unified_debugging/sqlite/metadata` | Metadata output. |
| `--raw-dir /out/unified_debugging/sqlite/raw` | Raw output. |

## 4. Chay metadata

Liet ke bug:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py --list
'
```

### 4.1. Clone / prepare repo

Neu chua clone repo nao, nen prepare repo truoc de tach loi network/clone khoi buoc
build/test metadata. Script se clone vao `/out/sqlite___sqlite`, tren host tuong
ung voi `defects4c/out_tmp_dirs/sqlite___sqlite`.

Chuan bi repo cho mot bug:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py \
    --prepare-repos \
    --clone \
    --sha 522ebfa7cee96fb325a22ea3a2464a63485886a8
'
```

Chuan bi repo cho toan bo 6 bug SQLite:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py \
    --prepare-repos \
    --clone
'
```

`--prepare-repos` chi clone/fetch va checkout repo, khong sinh metadata. Neu bo
qua buoc prepare, co the them `--clone` truc tiep vao lenh sinh metadata.

### 4.2. Sinh metadata cho mot bug

Smoke nhanh mot bug, output ghi vao thu muc tam:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py \
    --sha 522ebfa7cee96fb325a22ea3a2464a63485886a8 \
    --dual-run \
    --max-tests 1 \
    --metadata-dir /out/unified_debugging/sqlite/smoke_metadata \
    --raw-dir /out/unified_debugging/sqlite/smoke_raw
'
```

Chay mot bug vao output chinh:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py \
    --sha 522ebfa7cee96fb325a22ea3a2464a63485886a8 \
    --dual-run \
    --max-tests 5
'
```

Neu repo chua duoc prepare truoc do, them `--clone`.

### 4.3. Sinh metadata toan bo 6 bug

Smoke batch nhe:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py \
    --dual-run \
    --max-tests 5 \
    --skip-if-exists
'
```

Chay batch day du hon, co resume:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  cd /src/projects/sqlite___sqlite &&
  python3 build_meta_sqlite.py \
    --dual-run \
    --max-tests 50 \
    --skip-if-exists
'
```

Mac dinh, neu khong truyen `--sha` va `--limit`, script se xu ly tat ca bug
trong `bugs_list_new.json`.

Config khuyen nghi:

| Option | Khuyen nghi | Y nghia |
|---|---|---|
| `--dual-run` | bat | Phase A chay buggy + fixed, Phase B lay coverage buggy. |
| `--max-tests 50` | mac dinh | So test random lay them vao moi bug; dung `1` hoac `5` de smoke nhanh. |
| `--random-seed 20260428` | mac dinh | Dam bao random tests on dinh giua cac lan chay. |
| `--skip-if-exists` | bat khi chay batch | Resume neu metadata da ton tai va dung parser version hien tai. |
| `--sha <commit_after>` | optional | Chi chay mot bug theo `commit_after`. |
| `--skip-coverage` | debug nhanh | Bo Phase B, khong co `covered_functions`. |

## 5. Mapping sang Unified-Debugging

| Field | Cach script ghi |
|---|---|
| `bug_id` | `type.id` trong `bugs_list_new.json`; suffix `__<sha_after[:12]>` neu trung. |
| `dataset_name` | `"defects4c"`. |
| `language` | `"C"`. |
| `project` | `"sqlite___sqlite"`. |
| `source_file` | `<git_repo_dir_<bug_id>>/<files.src[0]>`. |
| `compile_cmd` | Lenh configure/make voi GCOV flags de debug. |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}`. |
| `tests[*].test_id` | Relpath Tcl test, vi du `test/gencol1.test`. |
| `tests[*].outcome` | Ket qua buggy version trong Phase A. |
| `tests[*].outcome_fixed` | Ket qua fixed version trong Phase A khi bat `--dual-run`. |
| `tests[*].covered_functions` | Coverage buggy version trong Phase B, format `"<file.c>:<func>"`. |
| `ground_truth_functions` | Parse hunk header tu `git diff commit_before..commit_after`, fallback theo location metadata. |
| `phase_info.coverage_parser_version` | Version parser coverage; dung de rerun metadata cu khi `--skip-if-exists`. |
| `phase_info.validation_status` | Check nhanh: `ok`, `no_related_fail`, `no_related_gt_coverage`, `no_coverage`, hoac `no_tests`. |

Luu y rieng cho SQLite coverage:

- SQLite build co the sinh va chay qua amalgamation `sqlite3.c`, nen GCOV thô
  doi khi bao function o `sqlite3.c`.
- Script scan cac source goc trong `src/`, `ext/misc`, `ext/fts3`,
  `ext/fts5`, `ext/rtree`, `ext/session` de tao mapping
  `function_name -> source_file`.
- Neu GCOV bao `sqlite3.c:lookupName`, script tra mapping va ghi metadata
  thanh `resolve.c:lookupName` neu tim thay `lookupName` trong `src/resolve.c`.
- Neu khong tim duoc source goc cho function, coverage giu fallback
  `sqlite3.c:<function>`.

## 6. Kiem tra nhanh

Sau khi chay mot bug, check `run_one_test.sh`:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  bash /out/sqlite___sqlite/git_repo_dir_CVE-2019-19317/run_one_test.sh gencol1
'
```

`run_one_test.sh` chap nhan ca `gencol1`, `gencol1.test`, `test/gencol1.test` neu file ton tai.
Voi `CVE-2019-19317`, `gencol1` segfault/fail tren buggy va pass tren fixed.

Check nhanh metadata:

```bash
docker exec my_defects4c_sqlite___sqlite bash -lc '
  python3 - <<PY
import json
p="/out/unified_debugging/sqlite/metadata/CVE-2019-19317_meta.json"
d=json.load(open(p))
gt=d.get("ground_truth_functions") or []
tests=d.get("tests") or []
related=[t for t in tests if t.get("outcome")=="FAIL" and t.get("outcome_fixed")=="PASS"]
hits=[]
for t in related:
    cov=t.get("covered_functions") or []
    if any(item.rsplit(":",1)[-1] in gt for item in cov if ":" in item):
        hits.append(t["test_id"])
print("source_file:", d.get("source_file"))
print("ground_truth_functions:", gt)
print("tests:", len(tests))
print("tests_with_coverage:", sum(1 for t in tests if t.get("covered_functions")))
print("related:", [t["test_id"] for t in related])
print("related_gt_hits:", hits)
print("phase_info:", d.get("phase_info"))
PY
'
```

Checklist toi thieu:

| Check | Ky vong |
|---|---|
| `source_file` | Tro toi file ton tai trong repo clone. |
| `ground_truth_functions` | Khong rong. |
| Related fail | Co test `outcome=FAIL` va `outcome_fixed=PASS` neu bug co triggering test trong set da chon. |
| Coverage | Co `covered_functions` cho ca PASS/FAIL tests khi Phase B build thanh cong. |
| Ground truth coverage | Related fail nen cover mot function trong `ground_truth_functions`. |
| `run_one_test.sh` | Chay doc lap duoc voi `{test_id}` trong `test_cmd_template`. |
