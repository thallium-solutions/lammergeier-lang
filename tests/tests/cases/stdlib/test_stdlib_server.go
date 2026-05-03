package main

import (
	"fmt"
)

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:15
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:16
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:18
func LogRequest(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:20
	// pass
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:23
func AddServerHeader(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:24
	res.SetHeader("X-Server", "lamserver/1")
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:27
func HelloHandler(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:28
	res.Text("hello world")
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:31
func UserHandler(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:32
	res.Text("user " + req.Params[":id"])
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:35
func EchoHandler(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:36
	res.Text(req.Body)
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:39
func AdminHandler(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:40
	res.Text("admin ok")
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:43
func AuthGuard(req *Request, res *Response) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:46
	if req.Path == "/admin" && req.Header("Authorization") != "Bearer s3cret" {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:47
		res.SetStatus(401)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:48
		res.Text("401 unauthorized")
	}
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:52
func AuthPlugin(srv *Server) {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:56
	srv.PreHandler(AuthGuard)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:57
	srv.Get("/admin", AdminHandler)
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:60
func main() {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:61
	var srv *Server = NewServer()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:64
	srv.OnRequest(LogRequest)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:65
	srv.OnResponse(AddServerHeader)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:68
	srv.Get("/hello", HelloHandler)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:69
	srv.Get("/user/:id", UserHandler)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:70
	srv.Post("/echo", EchoHandler)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:73
	srv.Register(AuthPlugin, "")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:75
	srv.ListenBackground(18181)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:78
	fmt.Println("GET /hello -> " + Http_get("http://127.0.0.1:18181/hello"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:79
	fmt.Println("GET /user/42 -> " + Http_get("http://127.0.0.1:18181/user/42"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:80
	fmt.Println("POST /echo -> " + Http_post("http://127.0.0.1:18181/echo", "text/plain", "ping"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:81
	fmt.Println("GET /admin (without auth) -> " + Http_get("http://127.0.0.1:18181/admin"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:84
	var headers map[string]string = map[string]string{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:85
	headers["Authorization"] = "Bearer s3cret"
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:86
	fmt.Println("GET /admin (with auth) -> " + Http_getWithHeaders("http://127.0.0.1:18181/admin", headers))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:89
	var serverHeader string = Http_getHeader("http://127.0.0.1:18181/hello", "X-Server")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:90
	var headerOk bool = serverHeader == "lamserver/1"
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:91
	fmt.Println(fmt.Sprintf("header X-Server present: %v", headerOk))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:94
	fmt.Println("404 path: " + Http_get("http://127.0.0.1:18181/no-such-path"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:96
	srv.Close()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_server.lam:97
	fmt.Println("server_tests_ok")
}

