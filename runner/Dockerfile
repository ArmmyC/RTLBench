ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG RTLBench_COMMIT
ARG RUNNER_CONFIG_VERSION
ARG PYTHON_VERSION
ARG IVERILOG_VERSION
ARG VVP_VERSION
ARG VERILATOR_VERSION
ARG YOSYS_VERSION

RUN test -n "${RTLBench_COMMIT}" \
    && test -n "${RUNNER_CONFIG_VERSION}" \
    && test -n "${PYTHON_VERSION}" \
    && test -n "${IVERILOG_VERSION}" \
    && test -n "${VVP_VERSION}" \
    && test -n "${VERILATOR_VERSION}" \
    && test -n "${YOSYS_VERSION}"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        iverilog="${IVERILOG_VERSION}" \
        verilator="${VERILATOR_VERSION}" \
        yosys="${YOSYS_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /opt/rtlbench/
COPY src /opt/rtlbench/src
COPY runner/runner_config.json /opt/rtlbench/runner_config.json

WORKDIR /opt/rtlbench
RUN groupadd --gid 65532 rtlbench \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin rtlbench \
    && mkdir -p /tmp/home /work \
    && chown 65532:65532 /tmp/home /work \
    && printf '%s\\n' '#!/bin/sh' 'exec python -m rtlbench.cli "$$@"' > /usr/local/bin/rtlbench \
    && chmod 0555 /usr/local/bin/rtlbench

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
