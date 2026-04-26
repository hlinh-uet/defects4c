# `php___php-src` - Defects4C x Unified-Debugging

## 1. Mục tiêu

Thư mục này chứa pipeline sinh metadata Unified-Debugging cho project
`php/php-src` trong Defects4C.

Input chính:

| File | Vai trò |
|---|---|
| `bugs_list_new.json` | Danh sách bug, gồm `commit_before`, `commit_after`, `files.src`, `files.test`, `c_compile.build_flags`, `type.id`. |
| `project.json` | Khai báo repo và template build/test Defects4C gốc. |
| `build_tpl.jinja` | Template build gốc của Defects4C cho php-src. |
| `test_tpl.jinja` | Template test gốc, chỉ chạy các `.phpt` trong `files.test`. |
| `build_meta_php.py` | Script sinh `*_meta.json` cho Unified-Debugging. |

Output chính:

| Output | Ý nghĩa |
|---|---|
| `/out/unified_debugging/php/metadata/{bug_id}_meta.json` | Metadata để Unified-Debugging đọc. |
| `/out/unified_debugging/php/raw/{bug_id}_meta.json` | Raw output, hiện cùng nội dung với metadata. |

Trên host, `/out/...` tương ứng với `defects4c/out_tmp_dirs/...`, nên output
thực tế nằm ở `defects4c/out_tmp_dirs/unified_debugging/php/...`.

## 2. Luồng xử lý của `build_meta_php.py`

Với mỗi bug trong `bugs_list_new.json`:

1. **Repo theo bug_id**
   * Script dùng `/out/php___php-src/git_repo_dir_<bug_id>`.
   * Nếu đã có repo cũ dạng `git_repo_dir_<commit_after>`, script tự rename
     sang tên theo `bug_id`.

2. **Fixed và buggy theo Defects4C gốc**
   * Fixed: checkout toàn repo về `commit_after`.
   * Buggy: checkout `commit_after`, sau đó chỉ checkout riêng `files.src` từ
     `commit_before`.
   * Vì vậy buggy là `fixed tree + buggy src overlay`, không phải toàn bộ repo
     ở `commit_before`.

3. **Bộ test**
   * Mặc định `--test-scope all`: lấy toàn bộ `.phpt` trong fixed/current tree.
   * Có thể dùng `--test-scope metadata` để chỉ chạy `.phpt` trong
     `bugs_list_new.json -> files.test`, phù hợp khi smoke test.
   * Danh sách test được giữ nguyên cho Phase A buggy, Phase A fixed và Phase B.

4. **Build**
   * Chạy `./buildconf --force`.
   * Chạy `./configure` với flags mặc định gần `build_tpl.jinja`, cộng thêm
     `c_compile.build_flags` của từng bug.
   * Phase A dùng ASAN để lấy outcome.
   * Phase B dùng GCOV trên buggy overlay để lấy `covered_functions`.

5. **Phase A**
   * Chạy buggy để ghi `tests[*].outcome`.
   * Nếu bật `--dual-run`, checkout fixed và chạy lại đúng bộ test đó để ghi
     `tests[*].outcome_fixed`.

6. **Phase B**
   * Checkout lại buggy overlay.
   * Build với `-fprofile-arcs -ftest-coverage`.
   * Trước từng test, xóa `*.gcda`.
   * Sau từng test, chạy `gcov` và ghi `tests[*].covered_functions`.
   * Nếu test crash quá sớm và không sinh `gcda`, script fallback từ ASAN stack
     trace nếu output có frame source-level.

7. **Metadata**
   * `bug_id` lấy từ `type.id`.
   * Nếu `type.id` trùng nhau, tên file có suffix `__<sha_after[:12]>`.
   * `raw/` và `metadata/` có cùng nội dung.

## 3. Docker + Run Flow

Project này dùng image riêng `defects4c/Dockerfile.php`, không cần image chung
nặng của Defects4C.

Lưu ý: các commit PHP 2016 cần Bison 2.7 để build Zend parser. Nếu container
cũ báo lỗi `yystrlen/yystpcpy` hoặc script báo cần Bison 2.7, hãy rebuild
`Dockerfile.php` và tạo lại container.

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
| `--metadata-dir /out/unified_debugging/php/metadata` | Metadata output trong container; trên host là `defects4c/out_tmp_dirs/unified_debugging/php/metadata`. |
| `--raw-dir /out/unified_debugging/php/raw` | Raw output trong container; trên host là `defects4c/out_tmp_dirs/unified_debugging/php/raw`. |

Config script khuyến nghị:

| Option | Khuyến nghị | Ý nghĩa |
|---|---|---|
| `--dual-run` | bật | Phase A chạy buggy + fixed; Phase B lấy coverage buggy. |
| `--test-scope metadata` | dùng khi smoke test | Chỉ chạy test trong `files.test`, nhanh hơn. |
| `--test-scope all` | dùng khi sinh full data | Chạy toàn bộ `.phpt` trong fixed/current tree. |
| `--gcov-scope all` | optional | Tương thích lệnh của tcpdump/cJSON; PHP thu coverage cho tập test đã chọn. |
| `--max-tests N` | optional | Giới hạn số test khi debug pipeline. |
| `--skip-if-exists` | bật khi chạy nhiều bug | Resume nếu metadata đã tồn tại. |
| `--sha <commit_after>` | optional | Chỉ chạy một bug theo `commit_after`. |

## 4. Bước 1: Build/Start Docker

### Trường hợp A: chạy lần đầu

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

docker build -f Dockerfile.php -t php-src/defect4c:latest .

docker rm -f my_defects4c_php 2>/dev/null || true
docker run -d --name my_defects4c_php \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  php-src/defect4c:latest sleep infinity
```

Clone dữ liệu commit của toàn bộ bug theo `bug_id`:

```bash
docker exec my_defects4c_php bash -lc '
  cd /src/projects/php___php-src && \
  python3 build_meta_php.py \
    --prepare-repos \
    --clone
'
```

### Trường hợp B: chạy các lần sau

Nếu image và container đã tồn tại, chỉ cần start lại container:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker start my_defects4c_php
```

Nếu container đã bị xóa, quay lại **Trường hợp A**.

## 5. Optional cleanup

Chỉ xóa output/log để chạy lại `build_meta_php.py`, không cần clone lại:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

rm -rf "out_tmp_dirs/unified_debugging/php/metadata"
rm -rf "out_tmp_dirs/unified_debugging/php/raw"
rm -rf "out_tmp_dirs/php___php-src/logs"
```

Xóa cả repo đã clone, cần chạy lại `--prepare-repos --clone`:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

rm -rf "out_tmp_dirs/php___php-src"
rm -rf "out_tmp_dirs/unified_debugging/php"
```

## 6. Chạy `build_meta_php.py`

Liệt kê bug:

```bash
docker exec my_defects4c_php bash -lc '
  cd /src/projects/php___php-src && \
  python3 build_meta_php.py --list
'
```

Sit1: Chạy full data cho một bug:

```bash
docker exec my_defects4c_php bash -lc '
  cd /src/projects/php___php-src && \
  python3 build_meta_php.py \
    --sha 28a6ed9f9a36b9c517e4a8a429baf4dd382fc5d5 \
    --metadata-dir /out/unified_debugging/php/metadata \
    --raw-dir /out/unified_debugging/php/raw \
    --dual-run \
    --gcov-scope all \
    --test-scope all
'
```

Sit2: Chạy toàn bộ bug, có resume:

```bash
docker exec my_defects4c_php bash -lc '
  cd /src/projects/php___php-src && \
  python3 build_meta_php.py \
    --metadata-dir /out/unified_debugging/php/metadata \
    --raw-dir /out/unified_debugging/php/raw \
    --dual-run \
    --gcov-scope all \
    --test-scope all \
    --skip-if-exists
'
```

Sit3: Chạy toàn bộ bug nhưng giới hạn test: 
```bash
docker exec my_defects4c_php bash -lc '
  cd /src/projects/php___php-src && \
  python3 build_meta_php.py \
    --sha 28a6ed9f9a36b9c517e4a8a429baf4dd382fc5d5 \
    --metadata-dir /out/unified_debugging/php/metadata \
    --raw-dir /out/unified_debugging/php/raw \
    --dual-run \
    --gcov-scope all \
    --test-scope all \
    --max-tests 40
'
```

Nếu thấy lỗi `Đang có process khác chạy`, kiểm tra process đang chạy:

```bash
docker exec my_defects4c_php bash -lc 'pgrep -af build_meta_php.py'
```

Nếu cần dừng run cũ rồi chạy lại:

```bash
docker exec my_defects4c_php bash -lc 'kill <pid>'
```

## 7. Mapping sang Unified-Debugging

| Field | Cách script ghi |
|---|---|
| `bug_id` | `type.id` trong `bugs_list_new.json`. |
| `source_file` | File đầu tiên trong `files.src`, path tuyệt đối trong repo đang xử lý. |
| `ground_truth_functions` | Best-effort từ hunk header của `git diff commit_before..commit_after -- files.src`. |
| `tests[*].outcome` | Kết quả chạy buggy overlay. |
| `tests[*].outcome_fixed` | Kết quả chạy fixed tree khi bật `--dual-run`. |
| `tests[*].covered_functions` | Coverage function của buggy overlay từ Phase B. |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}`. |
