# lamgreet

Minimal greeter for Lammergeier — ships as a seed in the local
development registry so `lamc install lamgreet` works immediately
after the registry container is up.

## Install

```
lamc install lamgreet
```

## Usage

```lammergeier
from lamgreet import hello, shout

func main() {
    print(hello("world"))   # hello, world!
    print(shout("world"))   # HELLO, WORLD!
}
```
