package main

import (
	"fmt"
)

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:54
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:56
func main() {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:57
	var r *Redis = Redis_connect("localhost:6379", "", 0, "")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:58
	r.FlushDb()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:60
	var pong string = r.Ping()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:61
	fmt.Println("ping: " + pong)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:64
	var missing string = r.Get("does-not-exist")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:65
	if len(missing) == 0 {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:66
		fmt.Println("get-missing: <empty>")
	} else {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:68
		fmt.Println("get-missing: " + missing)
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:70
	r.Set("greeting", "hello", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:71
	fmt.Println("set-and-get: " + r.Get("greeting"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:74
	var first bool = r.SetNx("nx-key", "hello", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:75
	var second bool = r.SetNx("nx-key", "world", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:76
	fmt.Println("setnx-first: " + fmt.Sprintf("%v", first))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:77
	fmt.Println("setnx-second: " + fmt.Sprintf("%v", second))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:78
	fmt.Println("get-after-setnx: " + r.Get("nx-key"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:81
	var ok bool = r.Expire("greeting", 60)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:82
	fmt.Println("ttl-set: " + fmt.Sprintf("%v", ok))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:83
	var t int = r.Ttl("greeting")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:84
	if t > 0 {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:85
		fmt.Println("ttl-positive: true")
	} else {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:87
		fmt.Println("ttl-positive: false")
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:89
	var persisted bool = r.Persist("greeting")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:90
	fmt.Println("persist: " + fmt.Sprintf("%v", persisted))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:91
	fmt.Println("ttl-after-persist: " + fmt.Sprintf("%v", r.Ttl("greeting")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:94
	fmt.Println("exists: " + fmt.Sprintf("%v", r.Exists([]interface{}{"greeting"})))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:95
	fmt.Println("delete: " + fmt.Sprintf("%v", r.Delete([]interface{}{"greeting"})))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:96
	fmt.Println("exists-after-delete: " + fmt.Sprintf("%v", r.Exists([]interface{}{"greeting"})))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:99
	fmt.Println("incr: " + fmt.Sprintf("%v", r.Incr("counter")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:100
	fmt.Println("incrBy-5: " + fmt.Sprintf("%v", r.IncrBy("counter", 5)))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:101
	fmt.Println("decr: " + fmt.Sprintf("%v", r.Decr("counter")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:102
	fmt.Println("counter-final: " + r.Get("counter"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:105
	var pushed int = r.Rpush("queue", []interface{}{"a", "b", "c"})
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:106
	fmt.Println("rpush-len: " + fmt.Sprintf("%v", pushed))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:107
	var rng []string = r.Lrange("queue", 0, (-1))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:108
	var joined string = ""
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:109
	for i := 0; i < len(rng); i += 1 {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:110
		if i > 0 {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:111
			joined = joined + "," + rng[i]
		} else {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:113
			joined = rng[i]
		}
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:116
	fmt.Println("lrange: " + joined)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:117
	fmt.Println("lpop: " + r.Lpop("queue"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:118
	fmt.Println("rpop: " + r.Rpop("queue"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:119
	fmt.Println("llen-after-pops: " + fmt.Sprintf("%v", r.Llen("queue")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:122
	var n int = r.Hset("user:1", "name", "alice")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:123
	fmt.Println("hset-1: " + fmt.Sprintf("%v", n))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:124
	var fields map[string]interface{} = map[string]interface{}{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:125
	fields["tier"] = "gold"
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:126
	fields["score"] = 5
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:127
	r.HsetMap("user:1", fields)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:128
	var profile map[string]string = r.HgetAll("user:1")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:129
	fmt.Println("hgetAll-name: " + profile["name"])
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:130
	fmt.Println("hgetAll-tier: " + profile["tier"])
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:131
	fmt.Println("hexists-name: " + fmt.Sprintf("%v", r.Hexists("user:1", "name")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:132
	fmt.Println("hexists-missing: " + fmt.Sprintf("%v", r.Hexists("user:1", "nope")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:133
	var incremented int = r.HincrBy("user:1", "score", 2)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:134
	fmt.Println("hincrBy: " + fmt.Sprintf("%v", incremented))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:135
	var keys []string = r.Hkeys("user:1")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:136
	fmt.Println("hkeys-count: " + fmt.Sprintf("%v", len(keys)))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:137
	fmt.Println("hdel-count: " + fmt.Sprintf("%v", r.Hdel("user:1", []interface{}{"name"})))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:140
	var added int = r.Sadd("tags", []interface{}{"go", "rust", "lam"})
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:141
	fmt.Println("sadd: " + fmt.Sprintf("%v", added))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:142
	fmt.Println("scard: " + fmt.Sprintf("%v", r.Scard("tags")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:143
	fmt.Println("sismember-go: " + fmt.Sprintf("%v", r.Sismember("tags", "go")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:144
	fmt.Println("sismember-php: " + fmt.Sprintf("%v", r.Sismember("tags", "php")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:145
	fmt.Println("smembers-count: " + fmt.Sprintf("%v", len(r.Smembers("tags"))))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:146
	fmt.Println("srem: " + fmt.Sprintf("%v", r.Srem("tags", []interface{}{"go"})))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:149
	fmt.Println("zadd-alice: " + fmt.Sprintf("%v", r.Zadd("scores", 100.0, "alice")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:150
	r.Zadd("scores", 90.0, "bob")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:151
	var newScore float64 = r.ZincrBy("scores", 5.0, "alice")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:152
	fmt.Println("zincrBy-alice: " + fmt.Sprintf("%v", int(newScore)))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:153
	fmt.Println("zscore-bob: " + fmt.Sprintf("%v", int(r.Zscore("scores", "bob"))))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:154
	var top []map[string]interface{} = r.ZrangeWithScores("scores", (-1), (-1))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:155
	var member string = ""
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:156
	if len(top) > 0 {
	if m, ok := top[0]["member"].(string); ok {
	member = m
	}
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:157
	fmt.Println("zrange-top: " + member)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:158
	fmt.Println("zrem: " + fmt.Sprintf("%v", r.Zrem("scores", []interface{}{"bob"})))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:159
	fmt.Println("zcard: " + fmt.Sprintf("%v", r.Zcard("scores")))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:162
	r.Set("scan:k1", "1", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:163
	r.Set("scan:k2", "2", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:164
	var cursorRes map[string]interface{} = r.Scan(0, "scan:*", 100)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:165
	var found int = 0
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:166
	if ks, ok := cursorRes["keys"].([]string); ok {
	found = len(ks)
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:167
	if found >= 1 {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:168
		fmt.Println("scan-found: 1")
	} else {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:170
		fmt.Println("scan-found: 0")
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:179
	var ps *PubSub = r.Subscribe([]interface{}{"events"})
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:180
	ps.SetTimeout(3000)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:181
	var publisher *Redis = Redis_connect("localhost:6379", "", 0, "")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:182
	publisher.Publish("events", "hello-subscribers")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:183
	publisher.Close()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:184
	var msg string = ps.Next()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:185
	if len(msg) > 0 {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:186
		fmt.Println("pubsub-message: " + msg)
	} else {
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:188
		fmt.Println("pubsub-message: <timeout>")
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:190
	ps.Close()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:193
	r.Set("pipekey", "pipeval", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:194
	r.Set("counter2", "10", 0)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:195
	var cmd1 map[string]interface{} = map[string]interface{}{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:196
	cmd1["cmd"] = "GET"
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:197
	var cmd1args []interface{} = []interface{}{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:198
	cmd1args = append(cmd1args, "pipekey")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:199
	cmd1["args"] = cmd1args
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:200
	var cmd2 map[string]interface{} = map[string]interface{}{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:201
	cmd2["cmd"] = "INCR"
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:202
	var cmd2args []interface{} = []interface{}{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:203
	cmd2args = append(cmd2args, "counter2")
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:204
	cmd2["args"] = cmd2args
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:205
	var pipe []map[string]interface{} = []map[string]interface{}{}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:206
	pipe = append(pipe, cmd1)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:207
	pipe = append(pipe, cmd2)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:208
	var results []interface{} = r.Pipeline(pipe)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:209
	var pipeGet string = ""
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:210
	var pipeIncr string = ""
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:211
	if len(results) > 0 {
	pipeGet = fmt.Sprintf("%v", results[0])
	}
	if len(results) > 1 {
	pipeIncr = fmt.Sprintf("%v", results[1])
	}
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:212
	fmt.Println("pipeline-get: " + pipeGet)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:213
	fmt.Println("pipeline-incr: " + pipeIncr)
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:216
	fmt.Println("type-string: " + r.Type_("pipekey"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:217
	fmt.Println("type-missing: " + r.Type_("never-existed"))
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:220
	r.FlushDb()
//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:221
	r.Close()
}

//line /home/spleen/Documents/code/lammergeier-lang/tests/tests/cases/stdlib/test_stdlib_lamredis.lam:224
