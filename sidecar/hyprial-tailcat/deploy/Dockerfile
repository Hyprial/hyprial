FROM golang:1.27.1 AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/hyprial-tailcat ./cmd/hyprial-tailcat
RUN mkdir -p /runtime/var/lib/hyprial-tailcat && chown 65532:65532 /runtime/var/lib/hyprial-tailcat

FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/hyprial-tailcat /usr/local/bin/hyprial-tailcat
COPY --from=build --chown=65532:65532 /runtime/var/lib/hyprial-tailcat /var/lib/hyprial-tailcat
USER 65532:65532
VOLUME ["/var/lib/hyprial-tailcat"]
ENTRYPOINT ["/usr/local/bin/hyprial-tailcat"]
