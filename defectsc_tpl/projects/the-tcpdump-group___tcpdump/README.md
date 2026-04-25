# `the-tcpdump-group___tcpdump` — Defects4C × Unified-Debugging

## 1. Kiến trúc & ngữ cảnh

```
Fault Localization/
├── defects4c/                                          ← framework Defects4C (fork)
│   ├── Dockerfile                                      (image gốc — NẶNG, cho mọi project)
│   ├── Dockerfile.tcpdump                              (★ image SLIM chỉ cho tcpdump)
│   ├── defectsc_tpl/
│   │   └── projects/
│   │       └── the-tcpdump-group___tcpdump/            ← THƯ MỤC NÀY
│   │           ├── bugs_list_new.json                  (nguồn dữ liệu bug)
│   │           ├── project.json                        (khai báo build/test)
│   │           ├── build_tpl.jinja                     (template build)
│   │           ├── test_tpl.jinja                      (template test)
│   │           ├── build_meta_tcpdump.py               (★ pipeline sinh metadata)
│   │           └── README.md                           (file này)
│   └── out_tmp_dirs/
│       └── the-tcpdump-group___tcpdump/
│           ├── git_repo_dir_<sha>/                     (cây nguồn đã clone)
│           └── logs/                                   (log build/test)
└── Unified-Debugging/                                  ← hệ thống FL + APR
    ├── data_loaders/defects4c_loader.py                (đọc *_meta.json)
    └── experiments/defects4c_cache/                    (cache source cho FL)
```

- `Defects4C` cung cấp cây mã nguồn + commit buggy/fixed và workflow Docker để
reproduce bug. 
- `Unified-Debugging` nhận đầu vào dạng **`{bug_id}_meta.json`**

---

## 2. Danh sách file trong thư mục

| File | Vai trò |
|---|---|
| `bugs_list_new.json` | Danh sách tất cả bug của tcpdump (commit_before / commit_after, files src & test, CVE). Là **input chính** cho pipeline. |
| `project.json` | Khai báo project: homepage, repo, template build/test, flags. |
| `build_tpl.jinja` | Jinja template dùng bởi Defects4C container để sinh `inplace_build.sh` / `inplace_rebuild.sh`. |
| `test_tpl.jinja` | Jinja template dùng để sinh `inplace_test.sh` — bản gốc chạy *cả batch* test với `run_test_testfilter`. |
| `build_meta_tcpdump.py` | **Script chính**: compile + chạy **từng** test case độc lập, thu coverage gcov, rồi ghi `{bug_id}_meta.json` cho Unified-Debugging. Metadata giữ nguyên toàn bộ test như raw. |

Các file template (`*.jinja`) phục vụ cho luồng Defects4C gốc (chạy qua Docker
`base/defect4c`). Script `build_meta_tcpdump.py` chạy **độc lập**, không phụ
thuộc Jinja, vì Unified-Debugging cần granularity **per-test** (để tính phổ
coverage Tarantula/Falcon cho FL và re-run từng test khi validate patch).

---

## 3. Pipeline của `build_meta_tcpdump.py`

Với mỗi bug trong `bugs_list_new.json`:

1. **Định vị repo** — dùng `defects4c/out_tmp_dirs/.../git_repo_dir_<sha_after>/`
   (được clone sẵn bởi `bulk_git_clone_v2.sh`). Nếu thiếu, dùng `--clone` để tự
   clone từ `https://github.com/the-tcpdump-group/tcpdump.git`.

2. **Checkout bản fixed** — `git reset --hard && git clean -fdx`, rồi
   `git checkout commit_after`. Đây là cây mã nguồn gốc mà Defects4C mặc định
   chuẩn bị khi clone repo.

3. **Tạo bản buggy theo kiểu Defects4C gốc** — từ fixed tree ở bước 2, script
   chỉ checkout riêng `src_file` về `commit_before`. Vì vậy repo buggy là
   **fixed tree + buggy src overlay**, không phải checkout toàn bộ repo về
   `commit_before`.

4. **Dùng bộ test của fixed tree**
   * `tests/TESTLIST`, `*.pcap`, `*.out` và các helper test vẫn đến từ
     `commit_after`, đúng như workflow mặc định của Defects4C.

5. **Build current tree để materialize TESTLIST và helper**
   * Checkout buggy overlay rồi configure + make.
   * Nếu thiếu `./configure` mà có `configure.ac`, script tự chạy
     `autoreconf -fi`.
   * Script sinh `run_one_test.sh` để APR có thể gọi lại đúng 1 test.

6. **Parse test list**
   * Parse `tests/TESTLIST` → danh sách `(name, input.pcap, expected.out, opts)`.
   * Với config mặc định hiện tại (`--max-pass -1`), script giữ toàn bộ
     `TESTLIST` của fixed/current tree để chạy.
   * Nếu bạn tự truyền `--max-pass`, script có thể giới hạn số test PASS ngoài
     regression subset.

7. **Phase A: thu outcome**
   * Chạy buggy version trên từng test như hiện tại và lưu `outcome`,
     `actual_output`, `expected_output`, `fail_reason`.
   * Nếu bật `--dual-run`, script checkout tiếp fixed version
     (`commit_after`), nhưng vẫn chạy lại đúng bộ test lấy từ fixed/current tree
     để lấy `outcome_fixed`.

8. **Phase B: thu coverage của buggy version**
   * Checkout lại buggy overlay, build non-ASAN + gcov:
     ```
     CFLAGS  = -g -O0 -fprofile-arcs -ftest-coverage
     LDFLAGS = -fprofile-arcs -ftest-coverage
     ```
   * Trước mỗi test: `find . -name '*.gcda' -delete` (reset coverage).
   * Chạy `./TESTonce <name> <input> <output> "<opts>"` trong `tests/`.
   * Với mỗi `*.gcda` vừa sinh, chạy `gcov -b -c` rồi parse các dòng
     `function NAME called N returned M`. Lấy các hàm có `N > 0`.
   * Kết quả lưu dạng `covered_functions: ["print-isakmp.c:isakmp_print", …]`
     — đúng format mà `Defects4CLoader._requalify_tests` kỳ vọng.

8. **Ghi metadata & helper**
   * Sinh `run_one_test.sh` trong cây build (lọc đúng 1 entry từ TESTLIST rồi
     gọi `TESTonce`) — để APR có thể validate patch bằng cách gọi
     `bash run_one_test.sh <test_id>`.
   * `bug_id` lấy từ `type.id` trong `bugs_list_new.json`.
   * Tên file output là `{safe_bug_id}_meta.json`; nếu `type.id` bị trùng giữa
     nhiều bug thì script tự thêm suffix `__<sha_after[:12]>` để tránh ghi đè.
   * Ghi cùng nội dung vào cả `raw/` và `metadata/`.

### 3.1 Ground-truth function (best-effort)

Script chạy `git diff <commit_before>..<commit_after> -- <src_files>` và bắt
tên hàm xuất hiện ngay sau `@@ … @@` (hunk header C) → trường
`ground_truth_functions` + `ground_truth` qualified. Dùng để tính metric
Top-K / MAP cho FL.

---

## 5. Prerequisites

### 5.1 Dữ liệu

* `bugs_list_new.json` đã có sẵn trong thư mục này.
* Cây nguồn tcpdump cho từng commit ở
  `defects4c/out_tmp_dirs/the-tcpdump-group___tcpdump/git_repo_dir_<sha>/` —
  **khuyến nghị** chạy `bash bulk_git_clone_v2.sh` trong container Defects4C
  một lần duy nhất để chuẩn bị (~vài GB). Nếu không có, dùng `--clone` (cần
  mạng).

### 5.2 Toolchain (Linux)

```bash
sudo apt-get install -y build-essential autoconf libpcap-dev gcc gcov git
python3 --version    # >= 3.8
```

Trên **macOS** không có `libpcap-dev` bản GNU và `gcov` của clang khác format
→ **nên chạy trong container Defects4C** (`my_defects4c_tcpdump`) thay vì chạy
trực tiếp trên host darwin.

### 5.3 Docker + Run Flow (khuyên dùng trên macOS)

1. cấu hình đường dẫn / tham số;
2. build hoặc start lại Docker;
3. optional: xóa cache/output cũ;
4. chạy `build_meta_tcpdump.py`.

#### 5.3.1 Config cần thống nhất trước khi chạy

Chạy từ thư mục:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
```

Các đường dẫn mount / output được dùng xuyên suốt:

| Biến / đường dẫn | Ý nghĩa |
|---|---|
| `$(pwd)/defectsc_tpl:/src` | source của Defects4C template trong container |
| `$(pwd)/out_tmp_dirs:/out` | nơi chứa repo đã clone, log build/test |
| `$(pwd)/unified_debugging:/unified_debugging` | nơi ghi `metadata/` và `raw/` |
| `$(pwd)/../Unified-Debugging:/udbg` | mount code Unified-Debugging để đối chiếu/debug |
| `--metadata-dir /out/unified_debugging/tcpdump/metadata` | output metadata trong container; trên host tương ứng là `defects4c/out_tmp_dirs/unified_debugging/tcpdump/metadata` |
| `--raw-dir /out/unified_debugging/tcpdump/raw` | output raw trong container; trên host tương ứng là `defects4c/out_tmp_dirs/unified_debugging/tcpdump/raw` |

Config chạy khuyến nghị cho script:

| Option | Khuyến nghị | Ý nghĩa |
|---|---|---|
| `--dual-run` | bật | phase A chạy buggy + fixed trên cùng bộ test của fixed/current tree để lấy `outcome` và `outcome_fixed`; phase B lấy coverage của buggy |
| `--gcov-scope all` | bật | thu coverage cho toàn bộ tập test đã chọn |
| `--skip-if-exists` | bật khi chạy nhiều bug | resume nếu đã có output |
| `--sha <commit_after>` | optional | chỉ chạy 1 bug theo `commit_after` để test pipeline |

> **Quan trọng:** mount `unified_debugging:/unified_debugging` là bắt buộc. Nếu
> thiếu mount này, file `*_meta.json` sẽ chỉ nằm trong container.

#### 5.3.2 Bước 1: chuẩn bị Docker

**Trường hợp A: chạy lần đầu**

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
mkdir -p unified_debugging

# Build image: chỉ cần làm 1 lần đầu hoặc khi sửa Dockerfile.tcpdump
docker build -f Dockerfile.tcpdump -t tcpdump/defect4c:latest .

# Tạo container chạy nền
docker rm -f my_defects4c_tcpdump 2>/dev/null || true
docker run -d --name my_defects4c_tcpdump \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  tcpdump/defect4c:latest sleep infinity

# Clone dữ liệu commit của tcpdump (chỉ cần làm khi out_tmp_dirs chưa có)
docker exec my_defects4c_tcpdump bash -lc \
  'cd /src && bash bulk_git_clone_v2.sh mini the-tcpdump-group___tcpdump'
```

**Trường hợp B: chạy các lần sau**

Nếu image và container đã tồn tại, chỉ cần khởi động lại container:

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker start my_defects4c_tcpdump
```

Nếu container đã bị xóa, quay lại **Trường hợp A**.

#### 5.3.3 Bước 2: optional xóa cache / output cũ

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

rm -rf "out_tmp_dirs/unified_debugging/tcpdump/metadata"
rm -rf "out_tmp_dirs/unified_debugging/tcpdump/raw"
rm -rf "out_tmp_dirs/the-tcpdump-group___tcpdump/logs"
```

#### 5.3.4 Bước 3: chạy `build_meta_tcpdump.py`

Đường dẫn script trong container:
`/src/projects/the-tcpdump-group___tcpdump/build_meta_tcpdump.py`

**Chạy thử 1 bug**

```bash
docker exec my_defects4c_tcpdump bash -lc '
  cd /src/projects/the-tcpdump-group___tcpdump && \
  python3 build_meta_tcpdump.py \
    --sha 5edf405d7ed9fc92f4f43e8a3d44baa4c6387562 \
    --metadata-dir /out/unified_debugging/tcpdump/metadata \
    --raw-dir /out/unified_debugging/tcpdump/raw \
    --dual-run \
    --gcov-scope all
'
```

**Chạy toàn bộ, có resume**

```bash
docker exec my_defects4c_tcpdump bash -lc '
  cd /src/projects/the-tcpdump-group___tcpdump && \
  python3 build_meta_tcpdump.py \
    --metadata-dir /out/unified_debugging/tcpdump/metadata \
    --raw-dir /out/unified_debugging/tcpdump/raw \
    --dual-run \
    --gcov-scope all \
    --skip-if-exists
'
```

> Nếu thấy lỗi `Đang có process khác chạy (lock: /tmp/.build_meta_tcpdump....lock)`,
> nghĩa là đã có một run khác đang chạy thật. Kiểm tra bằng:
>
> ```bash
> docker exec my_defects4c_tcpdump bash -lc 'pgrep -af build_meta_tcpdump.py'
> ```
>
> Nếu cần dừng run cũ rồi chạy lại:
>
> ```bash
> docker exec my_defects4c_tcpdump bash -lc 'kill <pid>'
> ```

---

## 9. Mapping nhanh sang chuẩn Unified-Debugging

| Unified-Debugging yêu cầu | Cách script đáp ứng |
|---|---|
| `bug_id` | `type.id` trong `bugs_list_new.json` |
| `dataset_name` | `"defects4c"` |
| `language` | `"C"` |
| `source_file` | `<git_repo_dir_<sha>>/<files.src[0]>` (absolute) |
| `compile_cmd` | `cd <repo> && CFLAGS=... LDFLAGS=... ./configure --prefix=<repo> && make -jN` |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}` |
| `tests[*].test_id` | Field 1 của dòng TESTLIST |
| `tests[*].outcome` | `PASS/FAIL` của buggy version trong phase A |
| `tests[*].outcome_fixed` | `PASS/FAIL` của fixed version (`commit_after`) khi chạy cùng bộ test lấy từ fixed/current tree |
| `tests[*].actual_output` | Content của `<test>.diff` (hoặc stdout/stderr tail) nếu fail |
| `tests[*].expected_output` | Nội dung file `<test>.out` nếu fail |
| `tests[*].covered_functions` | Coverage của buggy version trong phase B: parse `gcov -b -c` → `"<file.c>:<func>"` |
| `ground_truth_functions` | Parse hunk header của `git diff` |

Cầu nối validate patch nằm ở `Defects4CAdapter` trong
`data_loaders/sandbox_adapter.py`, nó dùng `bug_helper_v1_out2.py` trong
container để áp patch + rebuild + test.
