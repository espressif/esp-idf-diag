# Purge format description for esp-idf-diag

Once diagnostic information is collected, the purge file is utilized to remove
any sensitive data from the gathered files. By default, the purge file located
at `data/purge/purge.yml` is used unless it is specified
with the `--purge` argument, in which case the default file is not used.

## Overview of Purge Structure

It is a straightforward YAML file that includes a list of regular expressions
and the corresponding strings that should replace any matches.

    - regex: regular expression to look for
      repl: substitute string for match

The `regex.sub` function from Python is used internally.

## Purge variables

The `purge` file can utilize the following variables. The `esp-idf-diag` assigns
values to these variables and expands them in the `purge` file upon loading. To
use a variable, format it as `${NAME}`, such as `${IDF_PATH}`.

* USERNAME

    The username obtained using `getpass.getuser()`.
