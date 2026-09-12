# Optional local drill runner; never deployed as a public application service.
FROM docker:29.5.2-cli
RUN apk add --no-cache bash age python3 util-linux tar gzip
ENTRYPOINT ["bash"]
