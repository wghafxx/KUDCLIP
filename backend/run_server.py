"""Start the KudClip backend with a graceful, application-controlled shutdown."""

import argparse

import uvicorn

from server import app


THERMAL_EXIT_CODE = 75


def main():
    parser = argparse.ArgumentParser(description='Run the KudClip backend')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8001)
    options = parser.parse_args()
    server = uvicorn.Server(uvicorn.Config(app, host=options.host, port=options.port, timeout_graceful_shutdown=5))

    def request_shutdown():
        server.should_exit = True

    app.state.request_shutdown = request_shutdown
    server.run()
    if getattr(app.state, 'thermal_stop_reason', None):
        return THERMAL_EXIT_CODE
    return 0 if server.started else 1


if __name__ == '__main__':
    raise SystemExit(main())
