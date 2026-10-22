# The CommandLog/RollbackEntry models are deliberately NOT registered in the admin;
# the CRM does not need a 'Telegram bot' section. The log and rollbacks live in the DB
# (inspect them when needed via the shell or `rollback_command`).
