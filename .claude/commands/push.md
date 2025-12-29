---
description: Stage, commit and push changes to GitHub with a well-formatted commit message.
---

You will help the user commit and push their changes to GitHub following best practices.

## Commit message guidelines

If a commit message is provided, use it:
<commit_message>
$ARGUMENTS
</commit_message>

If no message is provided (empty or whitespace), you MUST generate one based on the changes.

## Instructions

Follow these steps carefully:

### 1. Analyze the current state

Run these commands in parallel:
- `git status` - to see all changes
- `git diff --staged` - to see staged changes
- `git diff` - to see unstaged changes
- `git log -3 --oneline` - to see recent commit style
- `git branch --show-current` - to identify current branch

### 2. Review and stage changes

- Review all modified, added, and deleted files
- If there are unstaged changes that should be included, stage them with `git add`
- NEVER stage files that contain secrets (.env, credentials, API keys, etc.)
- If you find potentially sensitive files, WARN the user and ask for confirmation

### 3. Generate or validate commit message

If no message was provided:
- Analyze the staged changes
- Generate a concise, descriptive commit message following conventional commits format:
  - `feat:` for new features
  - `fix:` for bug fixes
  - `docs:` for documentation
  - `style:` for formatting changes
  - `refactor:` for code refactoring
  - `test:` for tests
  - `chore:` for maintenance tasks
- The message should explain the "why" not just the "what"
- Keep the first line under 72 characters

### 4. Create the commit

Create the commit with this exact format (use HEREDOC):

```bash
git commit -m "$(cat <<'EOF'
<type>: <description>

<optional body explaining the changes>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

### 5. Push to remote

- Check if the branch has an upstream: `git status -sb`
- If no upstream, push with: `git push -u origin <branch-name>`
- If upstream exists, push with: `git push`
- NEVER use `--force` unless explicitly requested by the user

### 6. Confirm success

After pushing:
- Run `git status` to confirm clean state
- Show the user a summary: branch name, commit hash, and remote URL

## Safety rules

- NEVER push to `main` or `master` with `--force`
- NEVER commit files matching: `.env*`, `*credentials*`, `*secret*`, `*.pem`, `*.key`
- ALWAYS check the branch before pushing
- If there are no changes to commit, inform the user and stop

## Output

Provide a clean summary at the end:
```
Commit: <hash> (<branch>)
Message: <commit message first line>
Pushed to: <remote>/<branch>
```
