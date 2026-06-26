class HermesSession:
    def __init__(self, session_id=None):
        self.session_id = session_id
        self.active_messages = {}
        self.last_content_by_message_id = {}

    def reset_conversation(self):
        self.active_messages.clear()
        self.last_content_by_message_id.clear()

    def start_message(self, conversation_id, message_id):
        self.active_messages[message_id] = conversation_id
        self.last_content_by_message_id[message_id] = ""

    def conversation_for_message(self, message_id):
        return self.active_messages.get(message_id)

    def delta_for_edit(self, message_id, content):
        previous = self.last_content_by_message_id.get(message_id, "")
        if content.startswith(previous):
            delta = content[len(previous):]
        else:
            delta = content
        self.last_content_by_message_id[message_id] = content
        return delta

    def record_delta(self, message_id, delta):
        self.last_content_by_message_id[message_id] = (
            self.last_content_by_message_id.get(message_id, "") + delta
        )
