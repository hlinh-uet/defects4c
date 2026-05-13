# `nginx___njs` - Defects4C x Unified-Debugging

## 1. Muc tieu

Thu muc nay chua pipeline sinh metadata Unified-Debugging cho project
`nginx/njs` trong Defects4C.

Input chinh:

| File | Vai tro |
|---|---|
| `bugs_list_new.json` | Danh sach 11 CVE, gom `commit_before`, `commit_after`, `files.src`, `files.test`, `c_compile.*`, `type.id`. |
| `project.json` | Khai bao repo va template build/test Defects4C goc. |
| `build_tpl.jinja` | Template build goc cua Defects4C, build njs voi ASAN. |
| `test_tpl.jinja` | Template test goc cua Defects4C. Pipeline metadata hien chay `.c` bang `njs_unit_test`, `.t.js` bang `test/test262`, va PoC `.js` bang `build/njs`. |
| `test_tpl_cve_*.jinja` | Template override cho mot so CVE, hien dung PoC JS rieng. |
| `cve_*_poc.js` | PoC JS duoc chay bang `build/njs` khi bug co test template override. |
| `build_meta_nginx_njs.py` | Script sinh `*_meta.json` cho Unified-Debugging. |

Output chinh:

| Output | Y nghia |
|---|---|
| `/out/unified_debugging/nginx_njs/metadata/{bug_id}_meta.json` | Metadata de Unified-Debugging doc. |
| `/out/unified_debugging/nginx_njs/raw/{bug_id}_meta.json` | Raw output, hien cung noi dung voi metadata. |

Tren host, `/out/...` tuong ung voi `defects4c/out_tmp_dirs/...`.

## 2. Luong xu ly cua `build_meta_nginx_njs.py`

Voi moi bug trong `bugs_list_new.json`:

1. **Repo theo bug_id**
   * Script dung `/out/nginx___njs/git_repo_dir_<bug_id>`.
   * Neu da co repo cu dang `git_repo_dir_<commit_after>`, script rename sang ten theo
     `bug_id`, giong pipeline PHP.

2. **Fixed va buggy theo Defects4C goc**
   * Fixed: checkout toan repo ve `commit_after`.
   * Buggy: checkout `commit_after`, sau do chi checkout rieng `files.src` tu
     `commit_before`.
   * Vi vay buggy la `fixed tree + buggy src overlay`, khong phai toan bo repo o
     `commit_before`.

3. **Bo test**
   * `--test-scope metadata` la mac dinh: chay `files.test` va them PoC neu bug co
     `c_compile.test`.
   * `--test-scope all`: chay `metadata` + toan bo `test/**/*.t.js` trong fixed/current
     tree.
   * `--max-tests N` chi gioi han phan extra `test/**/*.t.js` cua scope `all`; khong cat
     test tu `files.test` hoac PoC override.

   Mapping test cua njs:

   | Metadata/test | Command |
   |---|---|
   | `src/test/njs_unit_test.c` | `build/njs_unit_test` voi test id `src/test/njs_unit_test.c`. |
   | `*.t.js` | `test/test262 --binary=build/njs <path>` voi test id la path file. |
   | PoC `*.js` | `build/njs <path>` voi test id `poc:<CVE-ID>`. |
   | `test_tpl_cve_*.jinja` | `build/njs cve_*_poc.js` voi test id `poc:<CVE-ID>`. |

4. **Build**
   * Phase A build voi ASAN de lay `outcome` va `outcome_fixed`.
   * Phase B build buggy overlay voi GCOV, khong ASAN, de lay `covered_functions`.
   * Phase B them generated header `/out/nginx___njs/_gcov_signal_flush/gcov_flush_on_signal.h`
     vao `--cc-opt=-include ...` de co gang flush `.gcda` khi test crash bang signal.
   * Luon build ca `build/njs` va `build/njs_unit_test`.

5. **Phase A**
   * Chay buggy de ghi `tests[*].outcome`.
   * Neu bat `--dual-run`, checkout fixed va chay lai dung bo test do de ghi
     `tests[*].outcome_fixed`.
   * Khong filter theo `buggy=FAIL, fixed=PASS`; unrelated fail van duoc luu.

6. **Phase B**
   * Checkout lai buggy overlay.
   * Build voi `-fprofile-arcs -ftest-coverage`.
   * Truoc moi test xoa `*.gcda`.
   * Sau moi test chay `gcov -f` va ghi `tests[*].covered_functions`.
   * Coverage luon la buggy coverage; khong fallback tu fixed tree hay ASAN output.

7. **Metadata**
   * `bug_id` lay tu `type.id`.
   * `raw/` va `metadata/` co cung noi dung.
   * File JSON duoc ghi atomic de tranh Ctrl-C de lai file rong.
   * `test_cmd_template` tro ve `bash <repo>/run_one_test.sh {test_id}`.

## 3. Docker + Run Flow

Project nay dung image rieng `defects4c/Dockerfile.nginx___njs`.

Chay tu thu muc:

```bash
cd "/path/to/defects4c"
```

### 3.1 Config duong dan

| Mount / option | Y nghia |
|---|---|
| `$(pwd)/defectsc_tpl:/src` | Source Defects4C template trong container. |
| `$(pwd)/out_tmp_dirs:/out` | Repo da clone, raw/metadata output, file GCOV flush generated. |
| `$(pwd)/patche_dirs:/patches` | Noi chua patch neu can validate APR. |
| `$(pwd)/../Unified-Debugging:/udbg` | Code Unified-Debugging. |
| `--out-root /out/nginx___njs` | Noi chua `git_repo_dir_<bug_id>` trong container; tren host la `defects4c/out_tmp_dirs/nginx___njs`. |
| `--metadata-dir /out/unified_debugging/nginx_njs/metadata` | Metadata output trong container; tren host la `defects4c/out_tmp_dirs/unified_debugging/nginx_njs/metadata`. |
| `--raw-dir /out/unified_debugging/nginx_njs/raw` | Raw output trong container; tren host la `defects4c/out_tmp_dirs/unified_debugging/nginx_njs/raw`. |

Config script khuyen nghi:

| Option | Khuyen nghi | Y nghia |
|---|---|---|
| `--dual-run` | bat | Phase A chay buggy + fixed; Phase B lay coverage buggy. |
| `--test-scope metadata` | dung khi smoke test | Chi chay `files.test` va PoC override, nhanh hon. |
| `--test-scope all` | dung khi muon mo rong | Chay `metadata` + extra `test/**/*.t.js`. |
| `--max-tests N` | optional | Chi gioi han phan extra `.t.js` cua `--test-scope all`. |
| `--skip-if-exists` | bat khi chay nhieu bug | Resume neu metadata da ton tai va dung version. |
| `--sha <commit_after>` | optional | Chi chay mot bug theo `commit_after`. |
| `--clone` | optional | Clone repo neu chua co repo local. |

### 3.2 Truong hop A: chay lan dau

Build image va tao container:

```bash
docker build -f Dockerfile.nginx___njs -t defects4c-nginx-njs:latest .

docker rm -f my_defects4c_nginx___njs 2>/dev/null || true
docker run -d --name my_defects4c_nginx___njs \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  defects4c-nginx-njs:latest sleep infinity
```

Clone/chuan bi repo cua toan bo bug theo `bug_id`:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --prepare-repos \
    --clone
'
```

Lenh tren tao repo dang:

```text
/out/nginx___njs/git_repo_dir_<bug_id>
```

Vi du tren host:

```text
defects4c/out_tmp_dirs/nginx___njs/git_repo_dir_CVE-2021-46461
```

Neu da co repo cu dang `git_repo_dir_<commit_after>`, script se tu rename sang
`git_repo_dir_<bug_id>`.

### 3.3 Truong hop B: chay cac lan sau

Neu image va container da ton tai, chi can start lai container:

```bash
docker start my_defects4c_nginx___njs
```

Neu container da bi xoa, quay lai **Truong hop A**.

Neu repo chua duoc prepare va ban chi muon chay mot bug, co the them `--clone`
vao lenh chay metadata. Neu khong co repo va khong them `--clone`, script se bao loi:

```text
Missing repo ... Run --prepare-repos --clone or add --clone.
```

## 4. Optional cleanup

Chi xoa metadata/raw de chay lai `build_meta_nginx_njs.py`, khong can clone lai repo:

```bash
cd "/path/to/defects4c"

rm -rf "out_tmp_dirs/unified_debugging/nginx_njs/metadata"
rm -rf "out_tmp_dirs/unified_debugging/nginx_njs/raw"
```

Xoa output generated cua njs nhung giu Unified-Debugging output:

```bash
cd "/path/to/defects4c"

rm -rf "out_tmp_dirs/nginx___njs"
```

Xoa ca repo da clone va metadata/raw, can chay lai `--prepare-repos --clone`:

```bash
cd "/path/to/defects4c"

rm -rf "out_tmp_dirs/nginx___njs"
rm -rf "out_tmp_dirs/unified_debugging/nginx_njs"
```

## 5. Chay `build_meta_nginx_njs.py`

Liet ke bug:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py --list
'
```

Chuan bi/clone repo neu chua co repo nao:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --prepare-repos \
    --clone
'
```

Chay smoke bug dau tien khi repo da prepare:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --sha d457c9545e7e71ebb5c0479eb16b9d33175855e2 \
    --metadata-dir /out/unified_debugging/nginx_njs/metadata \
    --raw-dir /out/unified_debugging/nginx_njs/raw \
    --dual-run \
    --test-scope metadata
'
```

Chay smoke mot bug khi repo chua prepare, cho phep script clone rieng bug do:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --sha d457c9545e7e71ebb5c0479eb16b9d33175855e2 \
    --metadata-dir /out/unified_debugging/nginx_njs/metadata \
    --raw-dir /out/unified_debugging/nginx_njs/raw \
    --dual-run \
    --test-scope metadata \
    --clone
'
```

Chay bug special da co repo clone:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --sha 39e8fa1b7db1680654527f8fa0e9ee93b334ecba \
    --metadata-dir /out/unified_debugging/nginx_njs/metadata \
    --raw-dir /out/unified_debugging/nginx_njs/raw \
    --dual-run \
    --test-scope metadata
'
```

Chay toan bo bug voi scope metadata, co resume:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --metadata-dir /out/unified_debugging/nginx_njs/metadata \
    --raw-dir /out/unified_debugging/nginx_njs/raw \
    --dual-run \
    --test-scope metadata \
    --skip-if-exists
'
```

Mo rong them `test/**/*.t.js`, gioi han 50 test extra:

```bash
docker exec my_defects4c_nginx___njs bash -lc '
  cd /src/projects/nginx___njs &&
  python3 build_meta_nginx_njs.py \
    --metadata-dir /out/unified_debugging/nginx_njs/metadata \
    --raw-dir /out/unified_debugging/nginx_njs/raw \
    --dual-run \
    --test-scope all \
    --max-tests 50 \
    --skip-if-exists
'
```

Neu thay loi `Dang co process khac chay`, kiem tra process dang chay:

```bash
docker exec my_defects4c_nginx___njs bash -lc 'pgrep -af build_meta_nginx_njs.py'
```

Neu can dung run cu roi chay lai:

```bash
docker exec my_defects4c_nginx___njs bash -lc 'kill <pid>'
```

## 6. Mapping sang Unified-Debugging

| Field | Cach script ghi |
|---|---|
| `bug_id` | `type.id` trong `bugs_list_new.json`. |
| `dataset_name` | `"defects4c"`. |
| `language` | `"C"`. |
| `project` | `"nginx___njs"`. |
| `source_file` | `<git_repo_dir_<bug_id>>/<files.src[0]>`. |
| `compile_cmd` | Lenh configure/make Phase A de debug. |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}`. |
| `tests[*].test_id` | Path tu `files.test`, path `.js/.t.js`, hoac `poc:<CVE-ID>`. |
| `tests[*].outcome` | Ket qua buggy version trong Phase A. |
| `tests[*].outcome_fixed` | Ket qua fixed version trong Phase A khi bat `--dual-run`. |
| `tests[*].covered_functions` | Coverage function cua buggy version trong Phase B, format `<file.c>:<func>`. |
| `ground_truth_functions` | Parse hunk header tu `git diff`; fallback bang `files.src*_location.func_start`. |
