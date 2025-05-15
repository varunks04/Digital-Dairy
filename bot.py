import os
import datetime
import requests
import logging
import shutil
from gtts import gTTS
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_DIARY, WAITING_FOR_AUDIO_CHOICE, WAITING_FOR_RATING = range(3)


# === Load configuration ===
def load_config():
    config = {
        "openrouter_api_key": "",
        "telegram_bot_token": "",
        "ai_model": "openai/gpt-3.5-turbo",
        "allowed_user_ids": []
    }

    try:
        with open("api_key.txt", "r") as f:
            config["openrouter_api_key"] = f.read().strip()
    except FileNotFoundError:
        logger.error("api_key.txt not found")

    try:
        with open("telegram_config.txt", "r") as f:
            lines = f.readlines()
            config["telegram_bot_token"] = lines[0].split("=")[1].strip()
    except FileNotFoundError:
        logger.error("telegram_config.txt not found")

    try:
        with open("telegram_config.txt", "r") as f:
            content = f.read()
            # Extract user IDs from ALLOWED_USER_IDS line
            if "ALLOWED_USER_IDS" in content:
                user_ids_str = content.split("ALLOWED_USER_IDS")[1].split("=")[1].strip()
                # Remove brackets, quotes and split by comma
                user_ids = user_ids_str.replace("[", "").replace("]", "").replace("\"", "").replace("'", "")
                config["allowed_user_ids"] = [uid.strip() for uid in user_ids.split(",")]
    except FileNotFoundError:
        logger.error("telegram_config.txt not found")

    return config


config = load_config()


# === User Authorization ===
def is_authorized_user(user_id):
    """Check if a user is authorized to use the bot."""
    return str(user_id) in config["allowed_user_ids"]


# === Prepare folder structure ===
def ensure_folders_exist(date_obj):
    month_folder = date_obj.strftime("%B")  # e.g., "May"
    diary_path = os.path.join("DATA", "Diary", month_folder)
    audio_path = os.path.join("DATA", "Audio", date_obj.strftime("%d-%m-%Y"))

    os.makedirs(diary_path, exist_ok=True)
    os.makedirs(audio_path, exist_ok=True)

    return diary_path, audio_path


# === Save diary entry ===
def save_diary_entry(user_id, entry_text):
    today = datetime.datetime.now()
    diary_path, _ = ensure_folders_exist(today)

    # Create user-specific file
    day_file = f"{today.strftime('%d')}_{user_id}.txt"
    file_path = os.path.join(diary_path, day_file)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(entry_text)

    return file_path


# === Load user bio ===
def load_user_bio(user_id):
    # Try to load user-specific bio
    user_bio_path = os.path.join("DATA", "Users", f"{user_id}_bio.txt")

    if os.path.exists(user_bio_path):
        with open(user_bio_path, "r", encoding="utf-8") as f:
            return f.read()

    # Fall back to default bio if user-specific one doesn't exist
    default_bio_path = os.path.join("DATA", "Bio.txt")
    try:
        with open(default_bio_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        # Return empty bio if no files exist
        logger.warning(f"No bio found for user {user_id}. Using empty bio.")
        return "No personal information available yet."


# === Analyze diary entry with AI ===
def analyze_day_with_openrouter(prompt_text):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['openrouter_api_key']}",
        "Content-Type": "application/json"
    }

    # Improved prompt structure for better analysis
    payload = {
        "model": config["ai_model"],
        "messages": [{"role": "user", "content": prompt_text}]
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"OpenRouter API Error: {e}")
        return "I'm sorry, I couldn't analyze your diary entry due to a technical issue."


# === Parse feedback into sections ===
def parse_feedback(text):
    sections = {
        "gratitude": "",
        "time_wasted": "",
        "good_use": "",
        "memorable_moments": "",
        "suggestions": "",
        "habit_patterns": "",
        "day_summary": "",
        "day_rating": "7"  # Default rating if none found
    }

    current_section = None

    # Try to find specifically formatted sections
    sections_to_find = {
        "gratitude": ["GRATITUDE:", "THINGS TO BE GRATEFUL FOR:"],
        "time_wasted": ["TIME INEFFICIENCY:", "TIME WASTED:"],
        "good_use": ["GOOD USE OF TIME:", "GOOD USE:"],
        "memorable_moments": ["MEMORABLE MOMENTS:"],
        "suggestions": ["SUGGESTIONS FOR IMPROVEMENT:", "SUGGESTIONS:"],
        "habit_patterns": ["HABIT PATTERN ANALYSIS:"],
        "day_summary": ["DAY SUMMARY", "DAY SUMMARY (AS A STORY):"],
        "day_rating": ["DAY RATING:", "RATING:"]
    }

    # First attempt: Look for each section specifically
    for section_key, possible_headers in sections_to_find.items():
        for header in possible_headers:
            if header in text:
                parts = text.split(header, 1)
                if len(parts) > 1:
                    # Find the end of this section (next section header or end of text)
                    section_content = parts[1]
                    end_pos = len(section_content)

                    # Check if any other section header appears after this one
                    for next_header in [h for headers in sections_to_find.values() for h in headers]:
                        if next_header in section_content:
                            pos = section_content.find(next_header)
                            if pos < end_pos:
                                end_pos = pos

                    # Extract just this section's content
                    sections[section_key] = section_content[:end_pos].strip()
                    break

    # Special handling for rating to ensure it's a number
    if sections["day_rating"]:
        # Try to extract just the numeric rating (e.g., "8/10" -> "8")
        import re
        rating_match = re.search(r'(\d+)(?:/10)?', sections["day_rating"])
        if rating_match:
            sections["day_rating"] = rating_match.group(1)
        else:
            sections["day_rating"] = "7"  # Default if we can't parse it

    # Ensure all sections have content
    for key in sections:
        if not sections[key]:
            if key == "day_rating":
                sections[key] = "7"  # Default rating
            else:
                sections[key] = "No specific points mentioned."

    return sections


# === Create audio files ===
def create_audio_files(sections, audio_path):
    audio_files = {}

    for section_name, content in sections.items():
        # Skip creating audio for the rating
        if section_name == "day_rating":
            continue

        filename = f"{section_name}.mp3"
        file_path = os.path.join(audio_path, filename)

        tts = gTTS(text=content, lang='en')
        tts.save(file_path)

        audio_files[section_name] = file_path

    return audio_files


# === Clean up audio files ===
def cleanup_audio_files(audio_path):
    """Delete audio files after they've been sent"""
    if os.path.exists(audio_path):
        try:
            shutil.rmtree(audio_path)
            logger.info(f"Cleaned up audio files in {audio_path}")
        except Exception as e:
            logger.error(f"Error cleaning up audio files: {e}")


# === Format message for Telegram ===
def format_section_message(title, content, date_str):
    return f"📅 *Daily Analysis for {date_str}*\n\n*{title}*\n\n{content}"


# === Telegram Bot Command Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return

    await update.message.reply_text(
        f"Hi {user.first_name}! I'm your Daily Reflection Bot.\n\n"
        "I'll help you track your daily activities and provide insights.\n"
        "Just say 'hi' or send /diary to get started!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return

    help_text = (
        "📝 *Daily Reflection Bot Commands*\n\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n"
        "/diary - Start a new diary entry\n"
        "/setbio - Set or update your personal bio (for better analysis)\n"
        "/mydiary - Show your recent diary entries\n"
        "/read YYYY-MM-DD - Read a specific diary entry\n\n"
        "Or just say 'hi' to begin a new daily reflection!"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def set_bio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set up user bio."""
    user = update.effective_user
    user_id = user.id

    # Check authorization
    if not is_authorized_user(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide your bio after the command. For example:\n"
            "/setbio I'm a software developer who loves running and reading."
        )
        return

    bio_text = " ".join(context.args)
    os.makedirs(os.path.join("DATA", "Users"), exist_ok=True)

    with open(os.path.join("DATA", "Users", f"{user_id}_bio.txt"), "w", encoding="utf-8") as f:
        f.write(bio_text)

    await update.message.reply_text("Your bio has been updated! I'll use this to provide more personalized analysis.")


# === Conversation flow handlers ===
async def handle_hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle 'hi' messages and start the diary conversation."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return ConversationHandler.END

    reply_keyboard = [["Skip - I'll type it"]]

    await update.message.reply_text(
        "Hello! How did your day go? Please share your activities, thoughts, and experiences.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )

    return WAITING_FOR_DIARY


async def start_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a new diary entry through command."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return ConversationHandler.END

    return await handle_hello(update, context)


async def process_diary_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the diary entry and generate analysis."""
    user = update.effective_user
    user_id = user.id
    diary_text = update.message.text

    if diary_text == "Skip - I'll type it":
        await update.message.reply_text(
            "Please type your diary entry for today:",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_FOR_DIARY

    # Acknowledge receipt
    processing_message = await update.message.reply_text("📝 Processing your diary entry...")

    # Save diary entry
    today = datetime.datetime.now()
    date_str = today.strftime("%d-%m-%Y")
    file_path = save_diary_entry(user_id, diary_text)
    diary_path, audio_path = ensure_folders_exist(today)

    # Load bio
    bio = load_user_bio(user_id)

    # Prepare improved prompt based on more practical, balanced assessment
    prompt = f"""You are a compassionate and balanced life coach who understands that being human means balancing productivity with rest, achievements with joy, and goals with reality. Analyze this daily narration with both wisdom and empathy.

USER BIO: {bio}

TODAY'S JOURNAL ENTRY ({date_str}): {diary_text}

Provide a balanced analysis with these clearly labeled sections:

GRATITUDE:
Identify 2-3 specific things from the day that deserve gratitude or appreciation, even if the day was challenging.

TIME INEFFICIENCY: 
Gently identify moments where time could have been used more effectively, but remember that not every minute needs to be productive. Be understanding that humans need downtime too.

GOOD USE OF TIME: 
Highlight specific periods that were productive, focused, meaningful, or even just restorative rest time. Note what made these moments valuable.

MEMORABLE MOMENTS: 
Point out any joyful, reflective, or learning-based events worth remembering from the day.

SUGGESTIONS FOR IMPROVEMENT: 
Offer 1-2 practical and realistic improvements:
- Focus on small, doable changes
- Suggest specific techniques when appropriate
- Balance ambition with self-compassion
- Include wisdom from various philosophies when they fit naturally

HABIT PATTERN ANALYSIS: 
Detect recurring habits (good or bad) and explain how they're shaping personal growth, without judgment.

DAY SUMMARY (AS A STORY): 
Write a refined, empathetic narrative of how the day unfolded:
- Use a human, reflective tone
- Preserve the sequence and emotions conveyed
- Balance achievements with human moments
- This is the version to be saved in the daily diary log

DAY RATING:
On a scale of 1-10, provide a balanced rating of the day, where 5-6 is a normal day, 10 is exceptional, and 1 is truly terrible. Include "/10" after the number.

Make each section clear with headers. Be direct but compassionate.
"""

    # Get AI analysis
    await update.message.reply_text("🔍 Analyzing your day...")
    feedback_text = analyze_day_with_openrouter(prompt)

    # Parse feedback
    sections = parse_feedback(feedback_text)

    # Save the complete feedback for reference
    feedback_path = os.path.join(diary_path, f"{today.strftime('%d')}_{user_id}_analysis.txt")
    with open(feedback_path, "w", encoding="utf-8") as f:
        f.write(feedback_text)

    # Ask about audio preference
    reply_keyboard = [["Yes, send audio", "No, text only"]]
    await update.message.reply_text(
        "Your diary entry has been analyzed! Would you like to receive the analysis as audio as well?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
    )

    # Store the analysis in context for later use
    context.user_data["analysis"] = {
        "sections": sections,
        "date_str": date_str,
        "audio_path": audio_path
    }

    return WAITING_FOR_AUDIO_CHOICE


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the command /start is issued."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return

    await update.message.reply_text(
        f"👋 Hi {user.first_name}! Welcome to your Daily Reflection Bot.\n\n"
        "I'll help you track your daily activities and provide thoughtful insights.\n\n"
        "Available commands:\n"
        "/diary - Start a new diary entry\n"
        "/setbio - Set your personal info for better analysis\n"
        "/mydiary - View your recent diary entries\n"
        "/help - Show all available commands\n\n"
        "You can also just say 'hi' to start a new diary entry!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a detailed help message when the command /help is issued."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return

    help_text = (
        "📔 *Daily Reflection Bot Commands*\n\n"
        "🚀 *Basic Commands*\n"
        "/start - Initialize the bot\n"
        "/help - Display this help message\n\n"
        "📝 *Diary Commands*\n"
        "/diary - Begin a new diary entry\n"
        "/mydiary - List your recent diary entries\n"
        "/read YYYY-MM-DD - View a specific diary entry\n\n"
        "👤 *Personal Settings*\n"
        "/setbio - Update your personal profile for better analysis\n\n"
        "💬 *Other Interactions*\n"
        "Just type 'hi' or 'hello' to start a new diary entry.\n\n"
        "ℹ️ Your entries will be analyzed to provide insights about your day, "
        "habit patterns, and suggestions for improvement."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def start_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin a new diary entry conversation flow."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user.id}) is not authorized to use this bot."
        )
        return ConversationHandler.END

    reply_keyboard = [["Skip - I'll type it"]]

    await update.message.reply_text(
        "📝 *New Diary Entry*\n\n"
        "How did your day go? Please share your activities, thoughts, and experiences.\n\n"
        "Be as detailed as you like - what you did, how you felt, what you learned, "
        "and any moments that stood out.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
        parse_mode="Markdown"
    )

    return WAITING_FOR_DIARY


async def set_bio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or update user's personal bio information."""
    user = update.effective_user
    user_id = user.id

    # Check authorization
    if not is_authorized_user(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot."
        )
        return

    if not context.args:
        # No arguments provided, show instructions
        current_bio = load_user_bio(user_id)

        await update.message.reply_text(
            "📋 *Personal Bio Setup*\n\n"
            "Your bio helps me provide more personalized analysis of your diary entries.\n\n"
            f"*Current bio:*\n{current_bio}\n\n"
            "*To update your bio:*\n"
            "Type `/setbio` followed by your information. For example:\n"
            "/setbio I'm a software developer who enjoys running, reading, "
            "and trying to maintain a healthy work-life balance.",
            parse_mode="Markdown"
        )
        return

    # Join all arguments into the bio text
    bio_text = " ".join(context.args)

    # Ensure user directory exists
    os.makedirs(os.path.join("DATA", "Users"), exist_ok=True)

    # Save the bio
    with open(os.path.join("DATA", "Users", f"{user_id}_bio.txt"), "w", encoding="utf-8") as f:
        f.write(bio_text)

    await update.message.reply_text(
        "✅ *Bio updated successfully!*\n\n"
        "I'll use this information to provide more personalized insights "
        "in your diary analysis.",
        parse_mode="Markdown"
    )


async def show_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display a list of user's recent diary entries."""
    user = update.effective_user
    user_id = user.id

    # Check authorization
    if not is_authorized_user(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot."
        )
        return

    # Path to diary entries
    diary_dir = os.path.join("DATA", "DiaryEntries")

    # Check if directory exists
    if not os.path.exists(diary_dir):
        await update.message.reply_text(
            "📭 *No entries found*\n\n"
            "You haven't created any diary entries yet.\n"
            "Use /diary to create your first entry!",
            parse_mode="Markdown"
        )
        return

    # Get all diary entries for this user
    entries = []
    for filename in os.listdir(diary_dir):
        if filename.endswith("_diary.txt"):
            # Extract date from filename
            date_str = filename.split('_')[0]

            # Try to open the file to check if it belongs to this user
            try:
                filepath = os.path.join(diary_dir, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Add basic validation to avoid listing files that don't belong to the user
                    entries.append((date_str, filename, filepath))
            except Exception:
                continue

    # Sort entries by date (newest first)
    entries.sort(reverse=True)

    if not entries:
        await update.message.reply_text(
            "📭 *No entries found*\n\n"
            "You haven't created any diary entries yet.\n"
            "Use /diary to create your first entry!",
            parse_mode="Markdown"
        )
        return

    # Display the most recent entries (limit to 10)
    message = "📚 *Your Recent Diary Entries:*\n\n"

    for date_str, filename, filepath in entries[:10]:
        try:
            # Format the date for display
            date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            formatted_date = date_obj.strftime('%A, %B %d, %Y')

            # Extract rating if available
            rating = "?"
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                    if "Day Rating:" in content:
                        rating_line = [line for line in content.split("\n") if "Day Rating:" in line]
                        if rating_line:
                            rating = rating_line[0].split("/")[0].split(":")[-1].strip()
            except Exception:
                pass

            # Add stars based on rating
            stars = ""
            if rating.isdigit():
                r = int(rating)
                if 1 <= r <= 10:
                    stars = "★" * r + "☆" * (10 - r)

            message += f"📝 *{formatted_date}*\n"
            message += f"   Rating: {rating}/10 {stars}\n"
            message += f"   Read: /read {date_str}\n\n"
        except ValueError:
            # Fallback for entries with invalid date format
            message += f"📝 Entry from {date_str}\n"
            message += f"   Read: /read {date_str}\n\n"

    message += "_To read a specific entry, use /read followed by the date (YYYY-MM-DD)_"

    await update.message.reply_text(message, parse_mode="Markdown")


async def read_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read and display a specific diary entry by date."""
    user = update.effective_user
    user_id = user.id

    # Check authorization
    if not is_authorized_user(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "📅 *Read Diary Entry*\n\n"
            "Please specify the date of the entry you want to read.\n\n"
            "*Format:* /read YYYY-MM-DD\n"
            "*Example:* /read 2025-05-15\n\n"
            "Use /mydiary to see a list of your available entries.",
            parse_mode="Markdown"
        )
        return

    date_str = context.args[0]

    # Try to validate the date format
    try:
        date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
        formatted_date = date_obj.strftime('%A, %B %d, %Y')
    except ValueError:
        await update.message.reply_text(
            "❌ *Invalid Date Format*\n\n"
            "Please use YYYY-MM-DD format.\n"
            "*Example:* /read 2025-05-15",
            parse_mode="Markdown"
        )
        return

    # Path to the specific diary entry
    diary_path = os.path.join("DATA", "DiaryEntries", f"{date_str}_diary.txt")

    if not os.path.exists(diary_path):
        await update.message.reply_text(
            f"❌ *Entry Not Found*\n\n"
            f"No diary entry found for {formatted_date}.\n\n"
            f"Use /mydiary to see a list of your available entries.",
            parse_mode="Markdown"
        )
        return

    # Read the diary entry
    try:
        with open(diary_path, "r", encoding="utf-8") as f:
            diary_content = f.read()
    except Exception as e:
        await update.message.reply_text(
            f"❌ *Error Reading Entry*\n\n"
            f"Could not read the diary entry: {str(e)}",
            parse_mode="Markdown"
        )
        return

    # Extract rating if available
    rating = "?"
    rating_line = None
    for line in diary_content.split("\n"):
        if "Day Rating:" in line:
            rating_line = line
            rating = line.split("/")[0].split(":")[-1].strip()
            break

    # Create a formatted header
    header = f"📔 *Diary Entry: {formatted_date}*\n\n"

    if rating.isdigit():
        r = int(rating)
        if 1 <= r <= 10:
            stars = "★" * r + "☆" * (10 - r)
            header += f"*Rating: {rating}/10*\n{stars}\n\n"

    # Remove the original header and rating line if present
    cleaned_content = diary_content
    if "Diary Entry:" in cleaned_content:
        cleaned_content = "\n".join(cleaned_content.split("\n")[1:])
    if rating_line:
        cleaned_content = cleaned_content.replace(rating_line, "")

    # Clean up any double newlines created
    while "\n\n\n" in cleaned_content:
        cleaned_content = cleaned_content.replace("\n\n\n", "\n\n")

    # Combine header and content
    formatted_content = header + cleaned_content

    # Send the diary entry in chunks if it's too long
    if len(formatted_content) > 4000:
        chunks = [formatted_content[i:i + 4000] for i in range(0, len(formatted_content), 4000)]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            else:
                await update.message.reply_text(
                    f"*(continued {i + 1}/{len(chunks)})*\n\n{chunk}",
                    parse_mode="Markdown"
                )
    else:
        await update.message.reply_text(formatted_content, parse_mode="Markdown")
async def send_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the analysis based on user's preference for audio."""
    audio_choice = update.message.text
    want_audio = audio_choice.startswith("Yes")

    analysis_data = context.user_data.get("analysis", {})
    sections = analysis_data.get("sections", {})
    date_str = analysis_data.get("date_str", datetime.datetime.now().strftime("%d-%m-%Y"))
    audio_path = analysis_data.get("audio_path", "")

    section_titles = {
        "gratitude": "🙏 Gratitude - Things to be thankful for",
        "time_wasted": "⏱️ Time Inefficiency - Where time could be better used",
        "good_use": "✅ Good Use of Time - Valuable periods",
        "memorable_moments": "🌟 Memorable Moments - Worth remembering",
        "suggestions": "📈 Gentle Suggestions for Improvement",
        "habit_patterns": "🔁 Habit Pattern Insights",
        "day_summary": "📝 Day Summary (as a Story)"
    }

    # Create audio files if requested (silently - no message)
    audio_files = {}
    if want_audio:
        # Create separate audio files for each section
        for section_key, content in sections.items():
            # Skip ratings for audio
            if section_key == "day_rating":
                continue

            # Make audio content brief and to the point - no introductions
            audio_filename = f"{section_key}.mp3"
            file_path = os.path.join(audio_path, audio_filename)

            # Create the audio file with just the content
            tts = gTTS(text=sections[section_key], lang='en')
            tts.save(file_path)

            audio_files[section_key] = file_path

    # Send each section
    for section_key, title in section_titles.items():
        content = sections.get(section_key, "No analysis available.")
        message = format_section_message(title, content, date_str)
        sent_msg = await update.message.reply_text(message, parse_mode="Markdown")

        # Send audio if requested - directly after each text section
        if want_audio and section_key in audio_files:
            with open(audio_files[section_key], "rb") as audio:
                # Send audio without any introduction text
                await update.message.reply_voice(audio, caption=f"{title.split('-')[0].strip()}")

    # Display the rating at the end with stars visualization
    rating = int(sections.get("day_rating", "7"))
    stars = "★" * rating + "☆" * (10 - rating)
    rating_message = f"📊 *Day Rating: {rating}/10*\n\n{stars}"
    await update.message.reply_text(rating_message, parse_mode="Markdown")

    # Also create a diary entry from the day summary
    today = datetime.datetime.now()
    diary_dir = os.path.join("DATA", "DiaryEntries")
    os.makedirs(diary_dir, exist_ok=True)

    # Format the diary entry filename with date and user ID
    diary_filename = f"{today.strftime('%Y-%m-%d')}_diary.txt"
    diary_file_path = os.path.join(diary_dir, diary_filename)

    # Get the day summary content
    day_summary = sections.get("day_summary", "No day summary available.")

    # Format the diary entry with header and rating
    diary_content = (
        f"Diary Entry: {today.strftime('%A, %B %d, %Y')}\n\n"
        f"Day Rating: {rating}/10\n\n"
        f"{day_summary}\n\n"
        f"Gratitude:\n{sections.get('gratitude', 'None noted.')}"
    )

    # Save the diary entry
    with open(diary_file_path, "w", encoding="utf-8") as f:
        f.write(diary_content)

    # Inform the user
    await update.message.reply_text(
        f"✍️ Your digital diary entry for {today.strftime('%A, %B %d')} has been saved."
    )

    # Clean up audio files if they were created
    if want_audio:
        cleanup_audio_files(audio_path)

    # Clear user data
    if "analysis" in context.user_data:
        del context.user_data["analysis"]

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel and end the conversation."""
    await update.message.reply_text(
        "Diary entry cancelled. You can start a new one anytime!",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def show_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the diary entries available for the user."""
    user = update.effective_user
    user_id = user.id

    # Check authorization
    if not is_authorized_user(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot."
        )
        return

    # Path to diary entries
    diary_dir = os.path.join("DATA", "DiaryEntries")

    # Check if directory exists
    if not os.path.exists(diary_dir):
        await update.message.reply_text("No diary entries found yet. Start by creating your first entry!")
        return

    # Get all diary entries for this user
    entries = []
    for filename in os.listdir(diary_dir):
        if f"_{user_id}_diary.txt" in filename:
            date_str = filename.split('_')[0]
            entries.append((date_str, filename))

    # Sort entries by date (newest first)
    entries.sort(reverse=True)

    if not entries:
        await update.message.reply_text("You don't have any diary entries yet. Start by creating your first entry!")
        return

    # Display the most recent entries (limit to 10)
    message = "*Your Recent Diary Entries:*\n\n"
    for date_str, filename in entries[:10]:
        # Format the date for display
        try:
            date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            formatted_date = date_obj.strftime('%A, %B %d, %Y')

            # Try to extract rating if present
            diary_path = os.path.join(diary_dir, filename)
            rating = "?"
            try:
                with open(diary_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if "Day Rating:" in content:
                        rating_line = [line for line in content.split("\n") if "Day Rating:" in line]
                        if rating_line:
                            rating = rating_line[0].split("/")[0].split(":")[-1].strip()
            except Exception:
                pass

            message += f"📝 {formatted_date} - Rating: {rating}/10\n"
        except ValueError:
            message += f"📝 {date_str}\n"

    message += "\nTo read a specific entry, use /read followed by the date (YYYY-MM-DD)"

    await update.message.reply_text(message, parse_mode="Markdown")


async def read_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read a specific diary entry."""
    user = update.effective_user
    user_id = user.id

    # Check authorization
    if not is_authorized_user(user_id):
        await update.message.reply_text(
            f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Please specify the date of the diary entry you want to read.\n"
            "Format: /read YYYY-MM-DD\n"
            "Example: /read 2025-05-15"
        )
        return

    date_str = context.args[0]

    # Try to validate the date format
    try:
        datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await update.message.reply_text(
            "Invalid date format. Please use YYYY-MM-DD format.\n"
            "Example: /read 2025-05-15"
        )
        return

    # Path to the specific diary entry
    diary_path = os.path.join("DATA", "DiaryEntries", f"{date_str}_{user_id}_diary.txt")

    if not os.path.exists(diary_path):
        await update.message.reply_text(f"No diary entry found for {date_str}.")
        return

    # Read the diary entry
    with open(diary_path, "r", encoding="utf-8") as f:
        diary_content = f.read()

    # Send the diary entry in chunks if it's too long
    if len(diary_content) > 4000:
        chunks = [diary_content[i:i + 4000] for i in range(0, len(diary_content), 4000)]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(f"(continued {i + 1}/{len(chunks)})\n{chunk}")
    else:
        await update.message.reply_text(diary_content)


def main():
    """Start the bot."""
    # Ensure required directories exist
    os.makedirs(os.path.join("DATA", "Diary"), exist_ok=True)
    os.makedirs(os.path.join("DATA", "Audio"), exist_ok=True)
    os.makedirs(os.path.join("DATA", "Users"), exist_ok=True)
    os.makedirs(os.path.join("DATA", "DiaryEntries"), exist_ok=True)

    # Check if default bio exists, create if not
    default_bio_path = os.path.join("DATA", "Bio.txt")
    if not os.path.exists(default_bio_path):
        with open(default_bio_path, "w", encoding="utf-8") as f:
            f.write("I am a person who values balance between productivity and happiness.")

    # Create the Application
    application = Application.builder().token(config["telegram_bot_token"]).build()

    # Add conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("diary", start_diary),
            MessageHandler(filters.Regex(r'^[Hh][Ii]$|^[Hh][Ee][Ll][Ll][Oo]$'), handle_hello)
        ],
        states={
            WAITING_FOR_DIARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_diary_entry)],
            WAITING_FOR_AUDIO_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_analysis)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    application.add_handler(conv_handler)

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("diary", start_diary))
    application.add_handler(CommandHandler("setbio", set_bio))
    application.add_handler(CommandHandler("mydiary", show_diary))
    application.add_handler(CommandHandler("read", read_diary))

    # Start the Bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
