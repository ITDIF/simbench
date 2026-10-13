"""S7 探针 v5：register_area 的 index 到底是 DB 号还是 WordLen？能不能挂两个 DB？"""
import ctypes, time
from snap7 import client as s7client, server as s7server, types

PORT = 1106
buf = (ctypes.c_ubyte * 64)()
buf[0:2] = (0x41, 0x48)

srv = s7server.Server()
print("register(DB, index=1, buf) ->", srv.register_area(types.srvAreaDB, 1, buf))
try:
    print("register(DB, index=2, buf2) ->", "ok",
          srv.register_area(types.srvAreaDB, 2, (ctypes.c_ubyte * 64)()))
except Exception as exc:
    print("register(DB, index=2, buf2) ->", type(exc).__name__ + ":", exc)
srv.start_to("127.0.0.1", PORT)
time.sleep(0.2)
cli = s7client.Client()
cli.connect("127.0.0.1", 0, 1, PORT)
try:
    for db in (1, 2, 3):
        try:
            print(f"  读 DB{db}[0:2] ->", cli.db_read(db, 0, 2).hex(" "))
        except Exception as exc:
            print(f"  读 DB{db} -> {type(exc).__name__}: {exc}")
    cli.db_write(1, 4, bytearray(b"\xab\xcd"))
    print("  主站写 DB1[4:6] -> 服务端缓冲:", bytes(buf[4:6]).hex(" "))
    buf[8] = 0x55
    print("  引擎侧写缓冲[8] -> 主站读:", cli.db_read(1, 8, 1).hex(" "))
finally:
    cli.disconnect(); srv.stop(); srv.destroy()
