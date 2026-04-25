# `DaveGamble___cJSON` - Defects4C x Unified-Debugging

## 1. Mục tiêu

Thư mục này chứa pipeline sinh metadata Unified-Debugging cho project
`DaveGamble/cJSON` trong Defects4C.

Input chính:

| File | Vai trò |
|---|---|
| `bugs_list_new.json` | Danh sách bug, gồm `commit_before`, `commit_after`, `files.src`, `files.test`, `type.id`. |
| `project.json` | Khai báo repo và template build/test Defects4C gốc. |
| `build_tpl.jinja` | Template build gốc, dùng CMake + Ninja. |
| `test_tpl.jinja` | Template test gốc của Defects4C; pipeline hiện tại không dùng nữa vì script tự discover full suite từ `ctest`. |
| `build_meta_cjson.py` | Script sinh `*_meta.json` cho Unified-Debugging. |

Output chính:

| Output | Ý nghĩa |
|---|---|
| `/out/unified_debugging/cjson/metadata/{bug_id}_meta.json` | Metadata để Unified-Debugging đọc. |
| `/out/unified_debugging/cjson/raw/{bug_id}_meta.json` | Raw output, hiện cùng nội dung với metadata. |

Trên host, `/out/...` tương ứng với `defects4c/out_tmp_dirs/...`, nên output
thực tế nằm ở `defects4c/out_tmp_dirs/unified_debugging/cjson/...`.

## 2. Luồng xử lý của `build_meta_cjson.py`

Với mỗi bug trong `bugs_list_new.json`:

1. **Tìm repo đã clone** tại `/out/DaveGamble___cJSON/git_repo_dir_<bug_id>`.
2. **Checkout fixed version** bằng `commit_after`.
3. **Tạo buggy version theo kiểu Defects4C gốc** bằng cách giữ fixed tree và
   chỉ checkout riêng `files.src` sang `commit_before`.
4. **Build current tree** bằng CMake trong thư mục `build_meta_cjson/`.
5. **Discover full test suite**
   * Script gọi `ctest --show-only=json-v1` trên build tree để lấy toàn bộ test
     mà project đã đăng ký.
   * Với cJSON hiện tại, full suite là 21 test (`cJSON_test`, `parse_*`,
     `print_*`, `misc_tests`, `json_patch_tests`, `misc_utils_tests`, ...).
6. **Phase A: lấy outcome**
   * Chạy toàn bộ test của current tree theo danh sách discover từ `ctest`.
   * Nếu bật `--dual-run`, checkout `commit_after`, build fixed version, nhưng
     vẫn chạy lại đúng bộ test đã lấy từ fixed/current tree để lấy `outcome_fixed`.
7. **Phase B: lấy coverage của buggy version**
   * Checkout lại buggy overlay.
   * Build với `GCOV+ASAN`.
   * Ưu tiên parse `gcov`; nếu process crash quá sớm và không sinh `gcda`, script
     fallback sang ASAN stack trace để giữ execution footprint tốt hơn `[]`.
8. **Ghi metadata**
   * `bug_id` lấy từ `type.id` trong `bugs_list_new.json`.
   * Nếu nhiều bug trùng `type.id`, tên file sẽ có suffix `__<sha_after[:12]>`.
   * `raw/` và `metadata/` có cùng nội dung.

Lưu ý về repo clone: script đặt tên repo theo `bug_id`, ví dụ
`git_repo_dir_CVE-2019-1010239`, để tránh nhầm với fixed commit. Repo này vẫn
fetch cả `commit_before` và `commit_after`; script sẽ checkout qua lại:

| Thời điểm | Commit được checkout | Mục đích |
|---|---|---|
| Sau `--prepare-repos --clone` | `commit_after` | Trạng thái mặc định sau bước chuẩn bị repo. |
| Phase A buggy | `commit_after` + overlay `files.src` từ `commit_before` | Chạy bộ test của fixed/current tree để lấy `tests[*].outcome`. |
| Phase A fixed | `commit_after` | Chạy lại cùng bộ test của fixed/current tree để lấy `tests[*].outcome_fixed`. |
| Phase B | `commit_after` + overlay `files.src` từ `commit_before` | Chạy lại cùng bộ test của fixed/current tree để lấy `tests[*].covered_functions`. |

Sau khi `build_meta_cjson.py --dual-run` chạy xong bình thường, repo được đưa
về lại buggy overlay. Nếu đã lỡ có repo cũ dạng `git_repo_dir_<commit_after>`,
script sẽ tự rename sang `git_repo_dir_<bug_id>` ở lần chạy tiếp theo.

## 3. Docker + Run Flow

Project này dùng image riêng `defects4c/Dockerfile.cjson`, tương tự cách
tcpdump dùng `Dockerfile.tcpdump`. Không cần build Dockerfile chung của
Defects4C.

Chạy từ thư mục:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
```

### 3.1 Config đường dẫn

| Mount / option | Ý nghĩa |
|---|---|
| `$(pwd)/defectsc_tpl:/src` | Source Defects4C template trong container. |
| `$(pwd)/out_tmp_dirs:/out` | Repo đã clone, log, raw/metadata output. |
| `$(pwd)/patche_dirs:/patches` | Nơi chứa patch nếu cần validate APR. |
| `$(pwd)/../Unified-Debugging:/udbg` | Code Unified-Debugging. |
| `--metadata-dir /out/unified_debugging/cjson/metadata` | Metadata output trong container; trên host là `defects4c/out_tmp_dirs/unified_debugging/cjson/metadata`. |
| `--raw-dir /out/unified_debugging/cjson/raw` | Raw output trong container; trên host là `defects4c/out_tmp_dirs/unified_debugging/cjson/raw`. |

Config script khuyến nghị:

| Option | Khuyến nghị | Ý nghĩa |
|---|---|---|
| `--dual-run` | bật | Phase A chạy buggy + fixed trên cùng bộ test của fixed/current tree; Phase B lấy coverage buggy. |
| `--gcov-scope all` | bật | Tương thích flow tcpdump; cJSON hiện chỉ có test executable nên chạy all. |
| `--skip-if-exists` | bật khi chạy nhiều bug | Resume nếu metadata đã tồn tại. |
| `--sha <commit_after>` | optional | Chỉ chạy một bug theo `commit_after`. |

### 3.2 Bước 1: Build/Start Docker

**Trường hợp A: chạy lần đầu**

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

docker build -f Dockerfile.cjson -t cjson/defect4c:latest .

docker rm -f my_defects4c_cjson 2>/dev/null || true
docker run -d --name my_defects4c_cjson \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  cjson/defect4c:latest sleep infinity
```

Clone dữ liệu commit của cJSON theo `bug_id`:

```bash
docker exec my_defects4c_cjson bash -lc '
  cd /src/projects/DaveGamble___cJSON && \
  python3 build_meta_cjson.py \
    --prepare-repos \
    --clone \
    --sha be749d7efa7c9021da746e685bd6dec79f9dd99b
'
```

**Trường hợp B: chạy các lần sau**

Nếu image và container đã tồn tại, chỉ cần start lại container:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker start my_defects4c_cjson
```

Nếu container đã bị xóa, quay lại **Trường hợp A**.

### 3.3 Bước 2: Optional cleanup

Chỉ xóa output/log để chạy lại `build_meta_cjson.py`, không cần clone lại:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

rm -rf "out_tmp_dirs/unified_debugging/cjson/metadata"
rm -rf "out_tmp_dirs/unified_debugging/cjson/raw"
rm -rf "out_tmp_dirs/DaveGamble___cJSON/logs"
```

Xóa cả repo đã clone, cần chạy lại `--prepare-repos --clone`:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

rm -rf "out_tmp_dirs/DaveGamble___cJSON"
rm -rf "out_tmp_dirs/unified_debugging/cjson"
```

### 3.4 Bước 3: Chạy `build_meta_cjson.py`

Liệt kê bug:

```bash
docker exec my_defects4c_cjson bash -lc '
  cd /src/projects/DaveGamble___cJSON && \
  python3 build_meta_cjson.py --list
'
```

Chạy thử một bug:

```bash
docker exec my_defects4c_cjson bash -lc '
  cd /src/projects/DaveGamble___cJSON && \
  python3 build_meta_cjson.py \
    --sha be749d7efa7c9021da746e685bd6dec79f9dd99b \
    --metadata-dir /out/unified_debugging/cjson/metadata \
    --raw-dir /out/unified_debugging/cjson/raw \
    --dual-run \
    --gcov-scope all
'
```

Chạy toàn bộ, có resume:

```bash
docker exec my_defects4c_cjson bash -lc '
  cd /src/projects/DaveGamble___cJSON && \
  python3 build_meta_cjson.py \
    --metadata-dir /out/unified_debugging/cjson/metadata \
    --raw-dir /out/unified_debugging/cjson/raw \
    --dual-run \
    --gcov-scope all \
    --skip-if-exists
'
```

Nếu thấy lỗi `Đang có process khác chạy`, kiểm tra process đang chạy:

```bash
docker exec my_defects4c_cjson bash -lc 'pgrep -af build_meta_cjson.py'
```

Nếu cần dừng run cũ rồi chạy lại:

```bash
docker exec my_defects4c_cjson bash -lc 'kill <pid>'
```

## 4. Mapping sang Unified-Debugging

| Field | Cách script ghi |
|---|---|
| `bug_id` | `type.id` trong `bugs_list_new.json`. |
| `dataset_name` | `"defects4c"`. |
| `language` | `"C"`. |
| `source_file` | `<git_repo_dir_<bug_id>>/<files.src[0]>`. |
| `compile_cmd` | Lệnh CMake build metadata ghi lại để debug. |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}`. |
| `tests[*].test_id` | Tên test discover từ `ctest`, ví dụ `cJSON_test`, `misc_tests`, `json_patch_tests`. |
| `tests[*].outcome` | Kết quả buggy version trong Phase A. |
| `tests[*].outcome_fixed` | Kết quả fixed version trong Phase A khi chạy cùng bộ test lấy từ fixed/current tree. |
| `tests[*].covered_functions` | Coverage buggy version trong Phase B, format `"<file.c>:<func>"`. |
| `ground_truth_functions` | Parse hunk header từ `git diff commit_before..commit_after`. |
