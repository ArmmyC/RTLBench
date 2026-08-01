ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG RTLBench_COMMIT
ARG RUNNER_CONFIG_VERSION
ARG PYTHON_VERSION
ARG IVERILOG_VERSION
ARG IVERILOG_PACKAGE_VERSION
ARG VVP_VERSION
ARG VERILATOR_VERSION
ARG VERILATOR_PACKAGE_VERSION
ARG YOSYS_VERSION
ARG YOSYS_PACKAGE_VERSION

RUN test -n "${RTLBench_COMMIT}" \
    && test -n "${RUNNER_CONFIG_VERSION}" \
    && test -n "${PYTHON_VERSION}" \
    && test -n "${IVERILOG_VERSION}" \
    && test -n "${IVERILOG_PACKAGE_VERSION}" \
    && test -n "${VVP_VERSION}" \
    && test -n "${VERILATOR_VERSION}" \
    && test -n "${VERILATOR_PACKAGE_VERSION}" \
    && test -n "${YOSYS_VERSION}" \
    && test -n "${YOSYS_PACKAGE_VERSION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        iverilog="${IVERILOG_PACKAGE_VERSION}" \
        verilator="${VERILATOR_PACKAGE_VERSION}" \
        yosys="${YOSYS_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /opt/rtlbench/
COPY src /opt/rtlbench/src
COPY runner/runner_config.json /opt/rtlbench/runner_config.json

RUN PYTHONPATH=/opt/rtlbench/src python -m rtlbench.build_identity \
    --python-version "${PYTHON_VERSION}" \
    --iverilog-package-version "${IVERILOG_PACKAGE_VERSION}" \
    --iverilog-version "${IVERILOG_VERSION}" \
    --vvp-version "${VVP_VERSION}" \
    --verilator-package-version "${VERILATOR_PACKAGE_VERSION}" \
    --verilator-version "${VERILATOR_VERSION}" \
    --yosys-package-version "${YOSYS_PACKAGE_VERSION}" \
    --yosys-version "${YOSYS_VERSION}"

WORKDIR /opt/rtlbench
RUN groupadd --gid 65532 rtlbench \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin rtlbench \
    && chown -R 0:0 /opt/rtlbench \
    && find /opt/rtlbench -type d -exec chmod 0555 {} + \
    && find /opt/rtlbench -type f -exec chmod 0444 {} + \
    && mkdir -p /tmp/home /work \
    && chown 65532:65532 /tmp/home /work \
    && printf '%s\n' \
        '#!/bin/sh' \
        'exec /usr/local/bin/python -m rtlbench.cli "$@"' \
        > /usr/local/bin/rtlbench \
    && chown 0:0 /usr/local/bin/rtlbench \
    && chmod 0555 /usr/local/bin/rtlbench \
    && test "$(sed -n '2p' /usr/local/bin/rtlbench)" = 'exec /usr/local/bin/python -m rtlbench.cli "$@"' \
    && test "$(stat -c '%u:%g' /opt/rtlbench)" = "0:0" \
    && test "$(stat -c '%a' /opt/rtlbench)" = "555" \
    && test "$(stat -c '%u:%g' /opt/rtlbench/src)" = "0:0" \
    && test "$(stat -c '%a' /opt/rtlbench/src)" = "555" \
    && test "$(stat -c '%u:%g' /opt/rtlbench/src/rtlbench)" = "0:0" \
    && test "$(stat -c '%a' /opt/rtlbench/src/rtlbench)" = "555" \
    && test "$(stat -c '%u:%g' /opt/rtlbench/src/rtlbench/cli.py)" = "0:0" \
    && test "$(stat -c '%a' /opt/rtlbench/src/rtlbench/cli.py)" = "444" \
    && test "$(stat -c '%u:%g' /usr/local/bin/rtlbench)" = "0:0" \
    && test "$(stat -c '%a' /usr/local/bin/rtlbench)" = "555"

ENV HOME=/tmp/home \
    TMPDIR=/tmp \
    PYTHONPATH=/opt/rtlbench/src \
    LANG=C \
    LC_ALL=C \
    PYTHONDONTWRITEBYTECODE=1

LABEL org.opencontainers.image.revision="${RTLBench_COMMIT}" \
      io.rtlbench.runner.config_version="${RUNNER_CONFIG_VERSION}" \
      io.rtlbench.python_version="${PYTHON_VERSION}" \
      io.rtlbench.iverilog_version="${IVERILOG_VERSION}" \
      io.rtlbench.vvp_version="${VVP_VERSION}" \
      io.rtlbench.verilator_version="${VERILATOR_VERSION}" \
      io.rtlbench.yosys_version="${YOSYS_VERSION}"

USER 65532:65532
RUN env -i \
    HOME=/tmp \
    TMPDIR=/tmp \
    LANG=C \
    LC_ALL=C \
    PYTHONPATH=/opt/rtlbench/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/local/bin/python -c \
    'import os; import pathlib; import rtlbench.cli; assert os.geteuid() == 65532; assert os.getegid() == 65532; assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"; path = pathlib.Path(rtlbench.cli.__file__).resolve(); assert str(path).startswith("/opt/rtlbench/src/rtlbench/"); assert path.is_file(); assert os.access(path, os.R_OK)' \
    && env -i \
    HOME=/tmp \
    TMPDIR=/tmp \
    LANG=C \
    LC_ALL=C \
    PYTHONPATH=/opt/rtlbench/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/local/bin/rtlbench --help >/dev/null
ENTRYPOINT ["rtlbench"]
