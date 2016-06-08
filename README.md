# Paul Winkler's git scripts

Various little helpers.  I currently use `git branch-activity` and `git
make-branch` the most, but have found the others occasionally helpful.

# TODO

Make them all self-documenting via `--help`


## git branch-activity

Show branches and date/time of most recent commit on each branch.
Most recent branches last.  Very helpful when there are lots of branches
and you remember you started working on it a month ago but forget what
you named the branch.

Example:

```
$ git branch-activity | tail -4
2016-05-31 11:11:00 -0400 8 days ago	remove_veeblefetzer_task
2016-06-07 14:03:30 -0400 20 hours ago	potrzebie_api_versioning
2016-06-07 14:08:51 -0400 20 hours ago	partner_form_copy_updates
2016-06-07 23:38:41 -0400 10 hours ago	master
```


## git branch-diff

Usage:
```
git branch-diff
git branch-diff <start-point>
git branch-diff -- [git-diff options]
```

Shows changes on the current branch since *start-point* (default
`origin/master`).

If you want to pass additional args to git-diff but not pick an explicit start
point, you can pass `--` as the start point as a synonym for `origin/master`.

TODO: Smarter default

Example:

```
$ git-branch-diff -- -w --minimal | wc -l
639
```

## git branch-log

Usage: `git branch-log [other-branch] [args...]`

Gives a log of commits on the current branch that are not on *other-branch*
(default `origin/master`).

Example:

```
$ git branch-log | grep "^commit " | wc -l
42
```

## git cat

Usage: `git cat <file> [<revision>]`

Dumps the contents of *file* at *revision* to stdout.
*revision* defaults to `HEAD`.

Example:

```
$ git cat README.md HEAD~4  | wc -l
93
```


## git make-branch

Automates my typical preferences for starting a new branch: branches
origin/master by default, without tracking, and creates a tag of the start
point.

Usage: `git make-branch branch-name [start_point] [tag_name]`

[start_point] is NOT tracked.
This is to avoid accidental pushes to the wrong remote branch.
Typically, `git push origin branch-name` is all you need.

If *start_point* is not given, origin/master is used.
If *tag_name* is not given, *<branch-name>_start_point_<date>*
is used.

## git search-all

Find commits where changes contain a search string.  Slow.

```
$ git-search-all check_metadata | head -n 2
58b20a6c477ce3438508554638ac677712dd6b88:api/foo/ident.py:def check_metadata(user, schema):
58b20a6c477ce3438508554638ac677712dd6b88:api/foo/tests/ident.py:        check_metadata True if have capability for object type
```

## git stash-branch

Intended as an alternative to `git stash`. I haven't used it much though.

You do `git stash-branch name-of-new-branch -a`, enter a commit message when
prompted, and then you're back on your original branch with all previously
uncommitted changes saved on `name-of-new-branch`.
