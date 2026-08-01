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
    && mkdir -p /tmp/home /work \
    && chown 65532:65532 /tmp/home /work \
    && printf '%s\n' \
        '#!/bin/sh' \
        'exec /usr/local/bin/python -m rtlbench.cli "$@"' \
        > /usr/local/bin/rtlbench \
    && chmod 0555 /usr/local/bin/rtlbench \
    && test "$(sed -n '2p' /usr/local/bin/rtlbench)" = 'exec /usr/local/bin/python -m rtlbench.cli "$@"' \
    && env -i \
        HOME=/tmp \
        TMPDIR=/tmp \
        LANG=C \
        LC_ALL=C \
        PYTHONPATH=/opt/rtlbench/src \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        /usr/local/bin/rtlbench --help >/dev/null \
    && env -i \
        HOME=/tmp \
        TMPDIR=/tmp \
        LANG=C \
        LC_ALL=C \
        PYTHONPATH=/opt/rtlbench/src \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        /usr/local/bin/python -c \
        'import rtlbench.cli; assert rtlbench.cli.__file__.startswith("/opt/rtlbench/src/rtlbench/")'

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
ENTRYPOINT ["rtlbench"]
