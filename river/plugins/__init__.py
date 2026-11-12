"""Checks that MaximizePM ships as plugins on the Monitoring Plugins API (#5462): a plugin check (maxpm ops add
<scope> <name> plugin --command "...") runs one of them as it runs any other executable. Each one prints a status
line, a line for each subject, and one JSON object of evidence, and exits 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN."""
