# Modes and slash commands: /tools, Tab, /tools in BUILD, /help.
Output media/demo-commands.gif

# Settings are inlined per tape: VHS drops Output commands from sourced
# tapes, and each tape must render standalone.
Set Shell bash
Set FontFamily "JetBrains Mono"
Set FontSize 18
Set Theme "Catppuccin Mocha"
Set Width 1100
Set Height 760
Set Padding 16
Set TypingSpeed 35ms
Set CursorBlink false
Set Framerate 24

Hide
Type "kill $(cat /tmp/ba-mock.pid) 2>/dev/null; rm -rf /tmp/ba-demo /tmp/demo-project /tmp/ba-mock.pid"
Enter
Type "python demo/mock_llm.py --scenario plan --port 8899 > /tmp/ba-mock.log 2>&1 & echo $! > /tmp/ba-mock.pid"
Enter
Type "mkdir -p /tmp/ba-demo && cp agent.py /tmp/ba-demo/agent.py && cp -r demo/workspace /tmp/demo-project && cd /tmp/ba-demo"
Enter
Sleep 500ms
# Launch BA hidden: agent.py clears the screen at startup, so the recording
# opens on the banner and prompt with nothing else above it.
Type "python agent.py --model demo-model --workspace /tmp/demo-project"
Enter
Sleep 2s
Show

Sleep 1s

Type "/tools"
Enter
Sleep 2.5s

Tab
Sleep 1s

Type "/tools"
Enter
Sleep 2.5s

Type "/models"
Enter
Sleep 2.5s

Type "/help"
Enter
Sleep 3.5s

Hide
Type "/exit"
Enter
Sleep 300ms
Type "kill $(cat /tmp/ba-mock.pid) 2>/dev/null; rm -rf /tmp/ba-demo /tmp/demo-project /tmp/ba-mock.pid"
Enter
