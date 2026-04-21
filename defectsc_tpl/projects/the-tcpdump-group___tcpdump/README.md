# `the-tcpdump-group___tcpdump` — Defects4C × Unified-Debugging

Thư mục này là **bộ mô tả project tcpdump** cho framework Defects4C và đồng thời
chứa pipeline sinh **metadata chuẩn Unified-Debugging** (`{bug_id}_meta.json`)
phục vụ cho Fault Localization (FL) và Automated Program Repair (APR).

---

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
| `build_meta_tcpdump.py` | **Script chính**: compile + chạy **từng** test case độc lập, thu coverage gcov, rồi ghi `{bug_id}_meta.json` cho Unified-Debugging. |

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

2. **Checkout bản buggy** — `git reset --hard && git clean -fdx`, rồi
   `git checkout commit_before`. Đây là trạng thái **chưa có** bản vá — bug
   còn nguyên.

3. **Khôi phục test asset** — các file `tests/TESTLIST`, `tests/*.pcap`,
   `tests/*.out` thuộc `files.test` thường được **thêm mới trong commit_after**
   (cùng lúc với bản vá). Script dùng `git show <sha_after>:<path>` để lấy các
   asset đó về cây buggy → test regression mới có chỗ để chạy.

4. **Compile với gcov** — configure + make với:
   ```
   CFLAGS  = -g -O0 -fprofile-arcs -ftest-coverage
   LDFLAGS = -fprofile-arcs -ftest-coverage
   ```
   Tạo file `.gcno` đi kèm mỗi object. Nếu thiếu `./configure` mà có
   `configure.ac`, script tự chạy `autoreconf -fi`.

5. **Chạy từng test case độc lập**
   * Parse `tests/TESTLIST` → danh sách `(name, input.pcap, expected.out, opts)`.
   * Chọn **tất cả regression test** khớp `*.pcap` của bug, cộng tối đa
     `--max-pass` test khác (default 50) để làm phổ coverage.
   * Trước mỗi test: `find . -name '*.gcda' -delete` (reset coverage).
   * Chạy `./TESTonce <name> <input> <output> "<opts>"` trong `tests/`.
   * Pass/Fail = `exit 0 && "TEST FAILED" ∉ output`. Nếu fail, đọc
     `<name>.diff` làm `actual_output`.

6. **Thu thập coverage (gcov)**
   * Với mỗi `*.gcda` vừa sinh, chạy `gcov -b -c` rồi parse các dòng
     `function NAME called N returned M`. Lấy các hàm có `N > 0`.
   * Kết quả lưu dạng `covered_functions: ["print-isakmp.c:isakmp_print", …]`
     — đúng format mà `Defects4CLoader._requalify_tests` kỳ vọng.

7. **Ghi metadata & helper**
   * Sinh `run_one_test.sh` trong cây build (lọc đúng 1 entry từ TESTLIST rồi
     gọi `TESTonce`) — để APR có thể validate patch bằng cách gọi
     `bash run_one_test.sh <test_id>`.
   * Xuất `<project>__<sha>_meta.json` vào
     `defects4c/unified_debugging/tcpdump/metadata/` (đường dẫn mặc định khớp
     với hằng `DEFECTS4C_TCPDUMP_METADATA_DIR`).

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

### 5.3 Docker (khuyên dùng trên macOS)

#### 5.3.1 Build image slim (1 lần duy nhất)

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"
docker build -f Dockerfile.tcpdump -t tcpdump/defect4c:latest .
```

#### 5.3.2 Chạy container

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

mkdir -p unified_debugging   # nơi lưu metadata {bug_id}_meta.json
docker rm -f my_defects4c_tcpdump 2>/dev/null || true
docker run -d --name my_defects4c_tcpdump \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  tcpdump/defect4c:latest sleep infinity
```

> **Quan trọng:** mount `unified_debugging:/unified_debugging` là **bắt buộc** —
> đó là nơi `build_meta_tcpdump.py` ghi file `*_meta.json`. Không mount thì
> metadata sẽ kẹt trong container và mất khi container bị xóa.


---

## 6. Cách chạy `build_meta_tcpdump.py`

Đường dẫn trong container: script nằm ở
`/src/projects/the-tcpdump-group___tcpdump/build_meta_tcpdump.py`.

### 6.1. Build

```bash
cd "/Users/linhnh/Documents/Fault Localization/defects4c"

# (a) Xoá container cũ (giữ nguyên image nếu đã build trước đó)
docker rm -f my_defects4c_tcpdump my_defects4c 2>/dev/null || true

# (b) Xoá dữ liệu cũ của tcpdump (clone, logs, metadata, patches)
rm -rf "out_tmp_dirs/the-tcpdump-group___tcpdump"
rm -rf "patche_dirs/the-tcpdump-group___tcpdump"
rm -rf "unified_debugging/tcpdump/metadata"
mkdir -p "unified_debugging"

# (c) Build image slim (chỉ cần làm 1 lần, hoặc khi sửa Dockerfile.tcpdump)
docker build -f Dockerfile.tcpdump -t tcpdump/defect4c:latest .

# (d) Khởi container mới (nền) — LƯU Ý: mount thêm unified_debugging
docker run -d --name my_defects4c_tcpdump \
  --ipc=host \
  -v "$(pwd)/defectsc_tpl:/src" \
  -v "$(pwd)/out_tmp_dirs:/out" \
  -v "$(pwd)/patche_dirs:/patches" \
  -v "$(pwd)/unified_debugging:/unified_debugging" \
  -v "$(pwd)/../Unified-Debugging:/udbg" \
  tcpdump/defect4c:latest sleep infinity

# (e) Clone dữ liệu các commit của tcpdump
docker exec my_defects4c_tcpdump bash -lc \
  'cd /src && bash bulk_git_clone_v2.sh mini the-tcpdump-group___tcpdump'

# (f) Thử 1 bug để xác nhận pipeline OK
docker exec my_defects4c_tcpdump bash -lc '
  cd /src/projects/the-tcpdump-group___tcpdump && \
  python3 build_meta_tcpdump.py \
    --sha 5edf405d7ed9fc92f4f43e8a3d44baa4c6387562 \
    --metadata-dir /out/unified_debugging/tcpdump/metadata \
    --raw-dir /out/unified_debugging/tcpdump/raw \
    --dual-run \
    --gcov-scope all
'

# (g) Nếu OK, chạy toàn bộ (resume được nếu bị ngắt)
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

### 6.2. Chạy

**Chạy trong container slim**
```bash
docker exec my_defects4c_tcpdump bash -lc \
  'cd /src/projects/the-tcpdump-group___tcpdump && python3 build_meta_tcpdump.py --skip-if-exists'
```

**Chạy thử 1 bug**
```bash
docker exec my_defects4c_tcpdump bash -lc \
  'cd /src/projects/the-tcpdump-group___tcpdump && \
   python3 build_meta_tcpdump.py --sha f76e7feb41a4327d2b0978449bbdafe98d4a3771 --max-pass 20'
```

---

## 9. Mapping nhanh sang chuẩn Unified-Debugging

| Unified-Debugging yêu cầu | Cách script đáp ứng |
|---|---|
| `bug_id` | `the-tcpdump-group___tcpdump@<commit_after>` |
| `dataset_name` | `"defects4c"` |
| `language` | `"C"` |
| `source_file` | `<git_repo_dir_<sha>>/<files.src[0]>` (absolute) |
| `compile_cmd` | `cd <repo> && CFLAGS=... LDFLAGS=... ./configure --prefix=<repo> && make -jN` |
| `test_cmd_template` | `bash <repo>/run_one_test.sh {test_id}` |
| `tests[*].test_id` | Field 1 của dòng TESTLIST |
| `tests[*].outcome` | `PASS/FAIL` theo TESTonce + `TEST FAILED` grep |
| `tests[*].actual_output` | Content của `<test>.diff` (hoặc stdout/stderr tail) nếu fail |
| `tests[*].expected_output` | Nội dung file `<test>.out` nếu fail |
| `tests[*].covered_functions` | Parse `gcov -b -c` → `"<file.c>:<func>"` |
| `ground_truth_functions` | Parse hunk header của `git diff` |

Cầu nối validate patch nằm ở `Defects4CAdapter` trong
`data_loaders/sandbox_adapter.py`, nó dùng `bug_helper_v1_out2.py` trong
container để áp patch + rebuild + test.
