FROM golang:1.27 AS build
WORKDIR /src
COPY go/go.mod go/go.sum ./
RUN go mod download
COPY go/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/s7poc ./cmd/s7poc

FROM gcr.io/distroless/static-debian12:nonroot
ENV CONFIG_PATH=/config/devices.yaml DASHBOARD_DIR=/dashboard
COPY --from=build /out/s7poc /s7poc
COPY dashboard /dashboard
EXPOSE 8002
ENTRYPOINT ["/s7poc"]
