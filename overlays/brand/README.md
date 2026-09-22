# Brand assets

Everything in this folder is served to the overlay pages at
`http://localhost:8778/brand/<file>`, which is how they reach it inside OBS.

## Your logo

Put it here and name it in your settings:

    [look]
    logo = "brand/my-logo.svg"

An SVG is best: the overlay is a web page, so an SVG stays sharp at any size and
is usually smaller than a PNG of the same mark. A PNG with real transparency
works just as well. The corner bug draws it at 36px tall and the holding cards at
232px, so a wide wordmark and a square badge both work: they are sized by height.

You can also point `logo` at a full URL if your art lives somewhere else, and
override it for one browser source with `?bug=<url>` on the overlay's URL.

With no logo set, the show's `name` is drawn as type instead. That is a complete
identity and needs no art at all.

## iRacing's logo

iRacing asks broadcasters to carry its logo, and the overlay has a place for it
in the bottom-left. Pylon does not ship the file, because it is iRacing's
trademark and not ours to redistribute: download the official one from iRacing's
media kit and save it here as `iracing-horizontal.svg`. Without it that corner is
simply empty.

## flags.woff2

A subset of **Noto Color Emoji** (SIL Open Font License 1.1, see `LICENSE-Noto`)
carrying the 26 regional indicators, the waving black flag and the tag
characters, which together make every national flag plus the UK home nations.

It is here because a driver's country reaches the tower as a flag emoji, and what
a browser draws for a pair of regional indicators is decided entirely by the
system emoji font. Windows has no colour flag emoji at all: without this file the
tower shows two letters instead of a flag on exactly the machine the broadcast
runs on.
