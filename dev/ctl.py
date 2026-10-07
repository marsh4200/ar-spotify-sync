import socket, sys
s = socket.create_connection(("127.0.0.1", 3999)); s.sendall((" ".join(sys.argv[1:]) + "\n").encode()); s.recv(16); s.close()
