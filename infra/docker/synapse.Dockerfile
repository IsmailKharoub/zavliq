ARG SYNAPSE_IMAGE=matrixdotorg/synapse:v1.160.0
FROM ${SYNAPSE_IMAGE}
COPY services/synapse-policy /opt/zavliq/policy
COPY infra/scripts/configure-synapse.py /opt/zavliq/configure-synapse.py
COPY infra/scripts/bootstrap-admin.py /opt/zavliq/bootstrap-admin.py
COPY infra/scripts/prune-media.py /opt/zavliq/prune-media.py
ENV PYTHONPATH=/opt/zavliq/policy
