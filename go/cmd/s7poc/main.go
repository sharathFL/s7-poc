package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rs/zerolog"

	"s7poc/internal/acq"
	"s7poc/internal/api"
	"s7poc/internal/config"
	"s7poc/internal/driver"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	// plc4go logs every protocol frame at trace/debug via zerolog; keep warnings and errors only.
	zerolog.SetGlobalLevel(zerolog.WarnLevel)
	cfg, err := config.Load()
	if err != nil {
		slog.Error("config", "error", err)
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	a := acq.New(cfg, driver.Factory)
	a.Start(ctx)

	dash := os.Getenv("DASHBOARD_DIR")
	if dash == "" {
		dash = "/dashboard"
	}
	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = ":8002"
	}
	srv := &http.Server{Addr: addr, Handler: (&api.Server{A: a, Dash: dash}).Handler(), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		slog.Info("listening", "addr", addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("http", "error", err)
			stop()
		}
	}()
	<-ctx.Done()
	sh, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(sh)
	a.Stop()
}
