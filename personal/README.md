# Personal Configuration

This directory holds user-specific configuration that AI agents read at runtime. All files except this README are git-ignored.

## Setup

Create a `git.md` file in this directory with your details:

```markdown
# Git Configuration

## Remote

- **GitHub Repository:** `https://github.com/<your-username>/automator`
- **SSH Remote:** `git@github.com:<your-username>/automator.git`
- **Authentication:** SSH using `~/.ssh/<your-key>.pub`
- **Primary Branch:** `master`

## Workflow

- Push with `git push` to sync with your remote.
```

You can add other files here for any tool-specific configuration you want agents to be aware of.
