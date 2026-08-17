# ----------------------------------------------------------------------
# Dockerfile.php - slim image cho Defects4C project php___php-src
#
# Image này phục vụ build_meta_php.py và run_debugging_case.py:
#   - autotools/build-essential để build php-src
#   - re2c/bison/pkg-config và các dev libs phổ biến cho extension metadata
#   - gcc/gcov để thu coverage
#   - python3 để chạy script sinh metadata và adapter Debugging Framework
#
# Build:
#   docker build -f Dockerfile.php -t php-src/defect4c:latest .
#
# Run:
#   docker run -d --name my_defects4c_php \
#     --ipc=host \
#     -v "$(pwd)/defectsc_tpl:/src" \
#     -v "$(pwd)/out_tmp_dirs:/out" \
#     -v "$(pwd)/patche_dirs:/patches" \
#     -v "$(pwd)/../Unified-Debugging:/udbg" \
#     php-src/defect4c:latest sleep infinity
# ----------------------------------------------------------------------
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    AM_I_IN_A_DOCKER_CONTAINER=Yes \
    TZ=UTC \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update -yq && \
    apt-get install -yq --no-install-recommends \
      build-essential make gcc g++ \
      autoconf automake libtool pkg-config bison re2c \
      git ca-certificates wget \
      jq \
      findutils coreutils grep sed diffutils gawk \
      util-linux bash \
      python3 python3-pip \
      tzdata \
      libxml2-dev libsqlite3-dev zlib1g-dev libpcre3-dev \
      libssl-dev libcurl4-openssl-dev \
      libpng-dev libjpeg-dev libwebp-dev libxpm-dev \
      libonig-dev libzip-dev libreadline-dev libedit-dev libicu-dev \
      libbz2-dev liblzma-dev && \
    apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir jinja2 jmespath

# php-src 5.x/7.0 era needs Bison 2.7 for generated Zend parsers. Newer
# Bison can produce parser objects that fail to link on these old commits.
RUN mkdir -p /tmp/bison-build && \
    wget -q https://ftp.gnu.org/gnu/bison/bison-2.7.tar.gz -O /tmp/bison-build/bison-2.7.tar.gz && \
    cd /tmp/bison-build && \
    tar -xf bison-2.7.tar.gz && \
    cd bison-2.7 && \
    wget -q 'https://raw.githubusercontent.com/rdslw/openwrt/e5d47f32131849a69a9267de51a30d6be1f0d0ac/tools/bison/patches/110-glibc-change-work-around.patch' -O- | git apply - && \
    ./configure --prefix=/opt/bison-2.7 && \
    make -j"$(nproc)" && \
    make install && \
    rm -rf /tmp/bison-build

COPY defectsc_tpl/projects/php___php-src/run_php_build.py \
     /usr/local/bin/defects4c-php-build
COPY defectsc_tpl/projects/php___php-src/run_php_tests.py \
     /usr/local/bin/defects4c-php-test
RUN chmod 0755 /usr/local/bin/defects4c-php-build \
               /usr/local/bin/defects4c-php-test

# Mount points:
#   /src     <- defectsc_tpl
#   /out     <- out_tmp_dirs
#   /patches <- patche_dirs
#   /udbg    <- Unified-Debugging
WORKDIR /src

CMD ["bash"]
