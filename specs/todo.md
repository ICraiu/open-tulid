- add base config file example in root of open-tulid
- add comments to the base config ( both to the base and to the one in .tulid )
- cannot create a project in a non empty projects thingy
$ tulid project test-project
projects must be a non-empty mapping

- even when the project piece is correctly done
$ tulid project test-project
Project is not configured: test-project

The expected behavior is that it will create the folder structure + empty workflow file + update the config so this project is tracked.