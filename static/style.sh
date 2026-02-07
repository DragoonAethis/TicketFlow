#!/usr/bin/env bash

# Use this script to update the trimmed down style.css with the latest Bootstrap files.
# You can add more stuff into style.sass too if you want to.

if [[ ! -d "scss" ]]; then
    echo "Can't find the 'scss' directory. Did you download the Bootstrap source files?"
    echo "- Go to https://getbootstrap.com"
    echo "- Download the source files"
    echo "- Unpack and place 'scss' directory next to this script"
    exit 1
fi

if [ ! -n "$(which sass)" ] ; then
    echo "Can't find the Dart Sass binary. Visit https://github.com/sass/dart-sass to set it up first."
    exit 1
fi

sass style.sass --style=compressed > style.css
