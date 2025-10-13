# Thor Deployment Ports

When running Brainbot inside Docker on the Thor setup, expose these ports so services remain reachable:

| Port | Service | Direction |
| --- | --- | --- |
| 6000 | Brainbot command service (ZeroMQ REQ/REP) | host ↔ container |
| 5555 | GR00T inference server (ZeroMQ REQ/REP) | host ↔ container |
| 7001 | Teleop action server (ZeroMQ REQ/REP, leader locale) | host ↔ container |
| 7002 | Teleop action server (ZeroMQ REQ/REP, AR controller) | host ↔ container |
| 7005 | Camera stream publisher (ZeroMQ PUB) | container → host |
| 8080 | WebViz HTTP dashboard | host ↔ container |

Example launch command:

```bash
docker run -it --runtime nvidia \
  --name brainbot \
  -p 6000:6000 \
  -p 5555:5555 \
  -p 7001:7001 \
  -p 7002:7002 \
  -p 7005:7005 \
  -p 8080:8080 \
  <image>
```
