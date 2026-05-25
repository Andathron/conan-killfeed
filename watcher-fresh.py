##[debug]Evaluating: secrets.RCON_HOST
##[debug]Evaluating Index:
##[debug]..Evaluating secrets:
##[debug]..=> Object
##[debug]..Evaluating String:
##[debug]..=> 'RCON_HOST'
##[debug]=> '***'
##[debug]Result: '***'
##[debug]Evaluating: secrets.RCON_PORT
##[debug]Evaluating Index:
##[debug]..Evaluating secrets:
##[debug]..=> Object
##[debug]..Evaluating String:
##[debug]..=> 'RCON_PORT'
##[debug]=> '***'
##[debug]Result: '***'
##[debug]Evaluating: secrets.RCON_PASSWORD
##[debug]Evaluating Index:
##[debug]..Evaluating secrets:
##[debug]..=> Object
##[debug]..Evaluating String:
##[debug]..=> 'RCON_PASSWORD'
##[debug]=> '***'
##[debug]Result: '***'
##[debug]Evaluating: secrets.DISCORD_WEBHOOK_KILLS
##[debug]Evaluating Index:
##[debug]..Evaluating secrets:
##[debug]..=> Object
##[debug]..Evaluating String:
##[debug]..=> 'DISCORD_WEBHOOK_KILLS'
##[debug]=> '***'
##[debug]Result: '***'
##[debug]Evaluating condition for step: 'Run kill feed watcher'
##[debug]Evaluating: success()
##[debug]Evaluating success:
##[debug]=> true
##[debug]Result: true
##[debug]Starting: Run kill feed watcher
##[debug]Loading inputs
##[debug]Loading env
Run python watcher.py
##[debug]/usr/bin/bash -e /home/runner/work/_temp/82e75fe2-34ed-4ccd-8f7f-c3e828404d64.sh
Traceback (most recent call last):
  File "/home/runner/work/conan-killfeed/conan-killfeed/watcher.py", line 215, in <module>
    main()
  File "/home/runner/work/conan-killfeed/conan-killfeed/watcher.py", line 195, in main
    current = get_max_rowid()
              ^^^^^^^^^^^^^^^
  File "/home/runner/work/conan-killfeed/conan-killfeed/watcher.py", line 121, in get_max_rowid
    raise RconError("Could not read MAX(rowid). Raw response was:\n" + raw)
RconError: Could not read MAX(rowid). Raw response was:

Error: Process completed with exit code 1.
##[debug]Finishing: Run kill feed watcher
