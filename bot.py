import os
import datetime
import requests
import logging
import shutil
import re
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
WAITING_FOR_DIARY, WAITING_FOR_AUDIO_CHOICE = range(2)


# === Load configuration ===
def load_config():
    """Load configuration from environment variables with proper validation."""
    config = {
        "openrouter_api_key": os.environ.get("OPEN_API_KEY", ""),
        "telegram_bot_token": os.environ.get("BOT_TOKEN", ""),
        "ai_model": os.environ.get("AI_MODEL", "openai/gpt-3.5-turbo"),
        "allowed_user_ids": []
    }

    # Load allowed user IDs from environment variable (as comma-separated string)
    allowed_ids = os.environ.get("ALLOWED_USER_IDS", "")
    if allowed_ids:
        config["allowed_user_ids"] = [uid.strip() for uid in allowed_ids.split(",") if uid.strip()]

    # Validate essential configuration
    if not config["telegram_bot_token"]:
        logger.error("BOT_TOKEN environment variable is not set!")

    if not config["openrouter_api_key"]:
        logger.error("OPEN_API_KEY environment variable is not set!")

    if not config["allowed_user_ids"]:
        logger.warning("No allowed user IDs specified. Bot will not be usable.")

    return config


config = load_config()


# === User Authorization ===
def is_authorized_user(user_id):
    """Check if a user is authorized to use the bot."""
    return str(user_id) in config["allowed_user_ids"]


# === Secure path operations ===
def ensure_folders_exist(date_obj):
    """Create necessary folder structure and return paths with proper sanitization."""
    month_folder = date_obj.strftime("%B")  # e.g., "May"
    diary_path = os.path.join("DATA", "Diary", month_folder)
    audio_path = os.path.join("DATA", "Audio", date_obj.strftime("%d-%m-%Y"))

    os.makedirs(diary_path, exist_ok=True)
    os.makedirs(audio_path, exist_ok=True)

    return diary_path, audio_path


# === Save diary entry with enhanced security ===
def save_diary_entry(user_id, entry_text):
    """Save diary entry with proper input sanitization."""
    today = datetime.datetime.now()
    diary_path, _ = ensure_folders_exist(today)

    # Sanitize user_id to ensure it's only digits
    safe_user_id = re.sub(r'[^\d]', '', str(user_id))

    # Create user-specific file
    day_file = f"{today.strftime('%d')}_{safe_user_id}.txt"
    file_path = os.path.join(diary_path, day_file)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(entry_text)

    return file_path


# === Load user bio with security measures ===
def load_user_bio(user_id):
    """Load user bio with proper input validation."""
    # Sanitize user_id to prevent path traversal attacks
    safe_user_id = re.sub(r'[^\d]', '', str(user_id))

    # Try to load user-specific bio
    user_bio_path = os.path.join("DATA", "Users", f"{safe_user_id}_bio.txt")

    if os.path.exists(user_bio_path) and os.path.isfile(user_bio_path):
        try:
            with open(user_bio_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error loading user bio: {e}")

    # Fall back to default bio if user-specific one doesn't exist
    default_bio_path = os.path.join("DATA", "Bio.txt")
    try:
        with open(default_bio_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        # Return empty bio if no files exist
        logger.warning(f"No bio found for user {user_id}. Using empty bio.")
        return "No personal information available yet."


# === Analyze diary entry with AI and improved error handling ===
def analyze_day_with_openrouter(prompt_text):
    """Analyze diary entry with OpenRouter API and robust error handling."""
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
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        logger.error("OpenRouter API request timed out.")
        return "I'm sorry, the analysis service took too long to respond. Please try again later."
    except requests.exceptions.RequestException as e:
        logger.error(f"OpenRouter API Request Error: {e}")
        return "I'm sorry, I couldn't analyze your diary entry due to a connection issue."
    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Error parsing API response: {e}")
        return "I'm sorry, there was an issue processing the analysis of your diary entry."
    except Exception as e:
        logger.error(f"Unexpected error in OpenRouter API call: {e}")
        return "I'm sorry, an unexpected error occurred while analyzing your diary entry."


# === Parse feedback into sections with enhanced robustness ===
def parse_feedback(text):
    """Parse AI feedback into organized sections with improved parsing logic."""
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
        rating_match = re.search(r'(\d+)(?:/10)?', sections["day_rating"])
        if rating_match:
            sections["day_rating"] = rating_match.group(1)
        else:
            sections["day_rating"] = "7"  # Default if we can't parse it

    # Ensure all sections have content and ratings are valid
    for key in sections:
        if not sections[key]:
            if key == "day_rating":
                sections[key] = "7"  # Default rating
            else:
                sections[key] = "No specific points mentioned."

    # Validate day rating is between 1-10
    try:
        rating = int(sections["day_rating"])
        if rating < 1 or rating > 10:
            sections["day_rating"] = "7"  # Default to 7 if out of range
    except ValueError:
        sections["day_rating"] = "7"  # Default to 7 if not a number

    return sections


# === Create audio files with better error handling ===
def create_audio_files(sections, audio_path):
    """Create audio files with improved error handling."""
    audio_files = {}

    for section_name, content in sections.items():
        # Skip creating audio for the rating
        if section_name == "day_rating":
            continue

        filename = f"{section_name}.mp3"
        file_path = os.path.join(audio_path, filename)

        try:
            tts = gTTS(text=content, lang='en')
            tts.save(file_path)
            audio_files[section_name] = file_path
        except Exception as e:
            logger.error(f"Error creating audio for {section_name}: {e}")
            # Continue with other sections even if one fails

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
    """Format message for Telegram with proper character escaping."""
    # Escape Markdown special characters to prevent formatting issues
    safe_content = content.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

    return f"📅 *Daily Analysis for {date_str}*\n\n*{title}*\n\n{safe_content}"


# === Telegram Bot Command Handlers ===
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

    # Maximum bio length for security
    if len(bio_text) > 2000:
        await update.message.reply_text(
            "❌ Bio is too long. Please keep it under 2000 characters."
        )
        return

    # Sanitize user_id to prevent path traversal attacks
    safe_user_id = re.sub(r'[^\d]', '', str(user_id))

    # Ensure user directory exists
    os.makedirs(os.path.join("DATA", "Users"), exist_ok=True)

    # Save the bio
    with open(os.path.join("DATA", "Users", f"{safe_user_id}_bio.txt"), "w", encoding="utf-8") as f:
        f.write(bio_text)

    await update.message.reply_text(
        "✅ *Bio updated successfully!*\n\n"
        "I'll use this information to provide more personalized insights "
        "in your diary analysis.",
        parse_mode="Markdown"
    )


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

    # Check if entry is too short
    if len(diary_text) < 10:
        await update.message.reply_text(
            "Your diary entry seems very short. Please provide a bit more detail for better analysis."
        )
        return WAITING_FOR_DIARY

    # Check if entry is too long
    if len(diary_text) > 10000:
        await update.message.reply_text(
            "Your diary entry is too long. Please keep it under 10,000 characters for effective analysis."
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
    # Sanitize user_id to prevent path traversal
    safe_user_id = re.sub(r'[^\d]', '', str(user_id))
    feedback_path = os.path.join(diary_path, f"{today.strftime('%d')}_{safe_user_id}_analysis.txt")

    try:
        with open(feedback_path, "w", encoding="utf-8") as f:
            f.write(feedback_text)
    except Exception as e:
        logger.error(f"Error saving analysis: {e}")

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
        try:
            # Create separate audio files for each section
            audio_files = create_audio_files(sections, audio_path)
        except Exception as e:
            logger.error(f"Error creating audio files: {e}")
            await update.message.reply_text(
                "Sorry, there was an issue creating the audio files. I'll send text analysis only."
            )
            want_audio = False

    # Send each section
    for section_key, title in section_titles.items():
        content = sections.get(section_key, "No analysis available.")

        # Limit content length to prevent message too long errors
        if len(content) > 3900:
            content = content[:3900] + "..."

        message = format_section_message(title, content, date_str)

        try:
            sent_msg = await update.message.reply_text(message, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            # Try without markdown if that fails
            try:
                sent_msg = await update.message.reply_text(
                    f"Daily Analysis for {date_str}\n\n{title}\n\n{content}"
                )
            except Exception as e2:
                logger.error(f"Error sending plain text message: {e2}")
                continue

        # Send audio if requested - directly after each text section
        if want_audio and section_key in audio_files:
            try:
                with open(audio_files[section_key], "rb") as audio:
                    # Send audio without any introduction text
                    await update.message.reply_voice(audio, caption=f"{title.split('-')[0].strip()}")
            except Exception as e:
                logger.error(f"Error sending audio file: {e}")

    # Display the rating at the end with stars visualization
    try:
        rating = int(sections.get("day_rating", "7"))
        if rating < 1 or rating > 10:
            rating = 7  # Default to 7 if out of range
    except ValueError:
        rating = 7  # Default to 7 if not a number

    stars = "★" * rating + "☆" * (10 - rating)
    rating_message = f"📊 *Day Rating: {rating}/10*\n\n{stars}"

    try:
        await update.message.reply_text(rating_message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error sending rating message: {e}")

    # Also create a diary entry from the day summary
    today = datetime.datetime.now()
    diary_dir = os.path.join("DATA", "DiaryEntries")
    os.makedirs(diary_dir, exist_ok=True)

    # Format the diary entry filename with date and user ID
    #safe_user_id = re.sub(r'[^\d]', '', str(user.id))
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
    try:
        with open(diary_file_path, "w", encoding="utf-8") as f:
            f.write(diary_content)

        # Inform the user
        await update.message.reply_text(
            f"✍️ Your digital diary entry for {today.strftime('%A, %B %d')} has been saved."
        )
    except Exception as e:
        logger.error(f"Error saving diary entry: {e}")
        await update.message.reply_text(
            "There was an issue saving your diary entry. Your analysis is still complete though!"
        )

    # Clean up audio files if they were created
    if want_audio:
        cleanup_audio_files(audio_path)

    # Clear user data to free up memory
    if "analysis" in context.user_data:
        del context.user_data["analysis"]

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel and end the conversation."""
    await update.message.reply_text(
        "Diary entry cancelled. You can start a new one anytime!",
        reply_markup=ReplyKeyboardRemove()
    )

    # Clear any stored data
    if "analysis" in context.user_data:
        del context.user_data["analysis"]

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

    # Get all diary entries
    entries = []
    try:
        for filename in os.listdir(diary_dir):
            if filename.endswith("_diary.txt"):
                # Extract date from filename
                date_match = re.match(r'(\d{4}-\d{2}-\d{2})_', filename)
                if date_match:
                    date_str = date_match.group(1)
                    entries.append((date_str, filename))
    except Exception as e:
        logger.error(f"Error reading diary directory: {e}")
        await update.message.reply_text("Error retrieving diary entries. Please try again later.")
        return

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
                    rating_match = re.search(r'Day Rating: (\d+)/10', content)
                    if rating_match:
                        rating = rating_match.group(1)
            except Exception as e:
                logger.error(f"Error reading diary file {filename}: {e}")

            # Add command to read this diary entry
            message += f"📆 *{formatted_date}* (Rating: {rating}/10)\n"
            message += f"  /read_{date_str.replace('-', '')}\n\n"
        except Exception as e:
            logger.error(f"Error processing diary entry {filename}: {e}")
            message += f"📆 *{date_str}*\n"
            message += f"  /read_{date_str.replace('-', '')}\n\n"

    message += "Use the commands above to read a specific entry."
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

    # Get the date from the command
    command = update.message.text
    date_match = re.search(r'/read_(\d{8})', command)

    if not date_match:
        await update.message.reply_text(
            "Please use the format /read_YYYYMMDD to read a specific diary entry."
        )
        return

    date_str = date_match.group(1)
    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    # Path to diary entry
    diary_filename = f"{formatted_date}_diary.txt"
    diary_path = os.path.join("DATA", "DiaryEntries", diary_filename)

    # Check if file exists
    if not os.path.exists(diary_path):
        await update.message.reply_text(f"No diary entry found for {formatted_date}.")
        return

    # Read the entry
    try:
        with open(diary_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Split content into chunks if too long for one message
        if len(content) > 4000:
            chunks = [content[i:i + 4000] for i in range(0, len(content), 4000)]
            await update.message.reply_text(f"📖 *Diary Entry: {formatted_date}*\n", parse_mode="Markdown")
            for chunk in chunks:
                await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(
                f"📖 *Diary Entry: {formatted_date}*\n\n{content}",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error reading diary file {diary_filename}: {e}")
        await update.message.reply_text(f"Error reading diary entry for {formatted_date}. Please try again later.")


async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unauthorized users."""
    user = update.effective_user
    user_id = user.id

    await update.message.reply_text(
        f"🚫 Access Denied. Your user ID ({user_id}) is not authorized to use this bot.\n\n"
        "Please contact the bot administrator if you believe this is an error."
    )


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unknown commands."""
    user = update.effective_user

    # Check authorization
    if not is_authorized_user(user.id):
        await handle_unauthorized(update, context)
        return

    await update.message.reply_text(
        "Sorry, I don't recognize that command. Use /help to see available commands."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors and log them."""
    logger.error(f"Exception while handling an update: {context.error}")

    # Send message to user only if update is available
    if update and isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Sorry, something went wrong. Please try again later."
        )


def main() -> None:
    """Set up and run the bot."""
    # Create the Application
    application = Application.builder().token(config["telegram_bot_token"]).build()

    # Create conversation handler for diary entries
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("diary", start_diary),
            MessageHandler(filters.Regex(r'^(hi|hello|hey)$'), handle_hello),
        ],
        states={
            WAITING_FOR_DIARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_diary_entry),
            ],
            WAITING_FOR_AUDIO_CHOICE: [
                MessageHandler(filters.Regex(r'^(Yes|No)'), send_analysis),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add the conversation handler
    application.add_handler(conv_handler)

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setbio", set_bio))
    application.add_handler(CommandHandler("mydiary", show_diary))

    # Add handler for diary read commands using regex
    application.add_handler(MessageHandler(filters.Regex(r'^/read_\d{8}$'), read_diary))

    # Add handler for unknown commands
    application.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    # Add error handler
    application.add_error_handler(error_handler)

    # Run the bot
    logger.info("Starting bot...")
    application.run_polling()


if __name__ == "__main__":
    # Create necessary directories if they don't exist
    os.makedirs(os.path.join("DATA", "Diary"), exist_ok=True)
    os.makedirs(os.path.join("DATA", "Audio"), exist_ok=True)
    os.makedirs(os.path.join("DATA", "Users"), exist_ok=True)
    os.makedirs(os.path.join("DATA", "DiaryEntries"), exist_ok=True)

    # Create default bio file if it doesn't exist
    default_bio_path = os.path.join("DATA", "Bio.txt")
    if not os.path.exists(default_bio_path):
        with open(default_bio_path, "w", encoding="utf-8") as f:
            f.write("No personal information available yet.")

    main()
