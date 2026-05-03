# @acme/lamcolor

Scoped-name example library bundled with the reference registry so
the ``@scope/name`` install + import + compile path is exercised
end-to-end.

## Install

```
lamc install @acme/lamcolor
```

## Usage

```lammergeier
from @acme/lamcolor import red, green, bold

func main() {
    print(red("error"))
    print(green(bold("ok")))
}
```
