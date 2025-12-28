import os
import re
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import anthropic
from dotenv import load_dotenv
load_dotenv()
from agent import load_documents, retrieve_documents, format_context_for_claude
import signal


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
CHANNEL_ID = os.environ.get("CHANNEL_ID")


# conversation history storage
conversations = {}

# Store any file ids that are shared here to access later via app.client.files_info  
file_ids = set()

# Path to help-center document information (JSON format)
DOCS_DIRECTORY = "doc_info"

#prompt without document context 
SYSTEM_PROMPT = """You an expert AI assistant in a Slack workspace.

Instructions:
- Focus on answering the user's most recent message
- Use prior messages in the conversation as context
- Be helpful but concise
- If the user's question is unclear, ask for clarification
"""

SYSTEM_PROMPT_WITH_CONTEXT = """You an expert AI assistant in a Slack workspace.

Instructions:
- Focus on answering the user's most recent message
- Use prior messages in the conversation as context
- Be helpful but concise
- If the user's question is unclear, ask for clarification

You have access to the following help center documentation:

{context}

Use this documentation to provide accurate answers. If you used the documentation, cite the title of the articles.
If the documentation doesn't cover the question, ignore it
"""


CLAUDE_MODEL = "claude-haiku-4-5-20251001"

def ask_claude(user_id: str, message: str) -> str:

    # Get or create conversation history for this user
    if user_id not in conversations:
        conversations[user_id] = []

    # Retrieve relevant documents, from agent.py 
    documents = retrieve_documents(message, max_docs=3)
    
    # Build system prompt
    if documents:
        context = format_context_for_claude(documents)
        system_prompt = SYSTEM_PROMPT_WITH_CONTEXT.format(context=context)
        logger.info(f"Added {len(documents)} documents to context")
    else:
        system_prompt = SYSTEM_PROMPT
    
    # Add user message, in standard claude format for messages 
    conversations[user_id].append({"role": "user", "content": message})
    
    # Keep only last 10 messages
    conversations[user_id] = conversations[user_id][-10:]
    
    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=conversations[user_id]
        )
        reply = response.content[0].text
        
        # Save reply to history
        conversations[user_id].append({"role": "assistant", "content": reply})
        
        return reply
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return f"API error: {e}"


# Handle DMs
@app.event("message")
def handle_dm(event, say, client):
    # Only respond to DMs, ignore bot messages
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    
    user_id = event["user"]
    channel_id = event["channel"]
    text = event.get("text", "")
    
    if text:
        # Post "thinking" message
        thinking_msg = client.chat_postMessage(
            channel=channel_id,
            text="Claude is Thinking..."
        )
        
        # Get response
        response = ask_claude(user_id, text)
        
        # Update with actual response
        client.chat_update(
            channel=channel_id,
            ts=thinking_msg["ts"],
            text=response
        )


# Handle @mentions
@app.event("app_mention")
def handle_mention(event, say, client):
    user_id = event["user"]
    channel_id = event["channel"]
    text = event.get("text", "")
    
    # Remove the @mention from the text
    message = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    
    if message:
        # Post "thinking" message
        thinking_msg = client.chat_postMessage(
            channel=channel_id,
            text="Claude is Thinking..."
        )
        
        # Get response
        response = ask_claude(user_id, message)
        
        # Update with actual response
        client.chat_update(
            channel=channel_id,
            ts=thinking_msg["ts"],
            text=response
        )
    else:
        say("Hi! How can I help?")

#file shared SAME behavior as file create
#WE WILL IMPROVE LATER 
@app.event("file_shared")
def handle_fileshare(event):
    file_id = event["file_id"]
    file_ids.add(file_id) 
    return #can improve later 

@app.event("file_created")
def handle_file_creation(event):
    file_id = event["file_id"]
    file_ids.add(file_id) 
    return #can improve later 

# /ask-claude command
@app.command("/ask-claude")
def ask_command(ack, command, say, client):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    text = command.get("text", "").strip()
    
    if not text:
        say("Usage: `/ask-claude [your question]`")
        return
    
    # Post "thinking" message
    thinking_msg = client.chat_postMessage(
        channel=channel_id,
        text="Claude is Thinking..."
    )
    
    # Get response
    response = ask_claude(user_id, text)
    
    # Update with actual response
    client.chat_update(
        channel=channel_id,
        ts=thinking_msg["ts"],
        text=response
    )


# /claude-reset command
@app.command("/claude-reset")
def reset_command(ack, command, say):
    ack()
    user_id = command["user_id"]
    conversations[user_id] = []
    say("Conversation reset!")


# Start the bot
if __name__ == "__main__":
    # Load documents on startup
    logger.info(f"Loading documents from: {DOCS_DIRECTORY}")
    load_documents(DOCS_DIRECTORY)

    if CHANNEL_ID:
        try:
            app.client.chat_postMessage(channel=CHANNEL_ID, text= " 🟢 Claude AI is online 🟢")
        except Exception as e:
            logger.warning(f"Couldnt post startup message {e}")

    def shutdown_handler(sig, frame):
        if CHANNEL_ID:
            try:
                app.client.chat_postMessage(channel=CHANNEL_ID, text= " 🔴 Claude AI is offline 🔴")
            except Exception as e:
                logger.warning(f"Could not post shutdown message {e}")
        exit(0)
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    logger.info("Bot starting...")
    handler.start()