import os
import json
import logging
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify, send_from_directory
)
from werkzeug.utils import secure_filename
# Assuming document_generator is available for notes/docx saving
from modules import document_generator 
import google.generativeai as genai # CRITICAL: Required for chat
import traceback # Included for robust error logging
# Import specific types needed for serialization handling
# REMOVED: from google.generativeai.types import Content # <-- THIS LINE CAUSED THE ImportError

# --- Environment Setup (CRITICAL FIX FOR AI RESPONSE) ---
# NOTE: This ensures the Gemini client is configured before the chat_model instance is created below.
api_key_chat = os.getenv("GEMINI_API_KEY")
if api_key_chat:
    try:
        genai.configure(api_key=api_key_chat)
        logging.info("Gemini API configured for chat routes.")
    except Exception as e:
        logging.error(f"FATAL: Gemini API configuration failed in routes.py: {e}")
# --------------------------------------------------------


# --- Local Imports ---
from . import db
# Ensure all models are imported (adjust if your model names are different)
from .models import User, Course, Video, Quiz, QuizHistory 

# --- Module Imports ---
from modules import text_generation, image_handling, text_to_speech, video_search
from dotenv import load_dotenv

load_dotenv()

# bind available quiz-generation function from modules.text_generation
generate_quiz_func = None
for _name in ("generate_quiz_json", "generate_quiz", "create_quiz"):
    candidate = getattr(text_generation, _name, None)
    if callable(candidate):
        generate_quiz_func = candidate
        break

# --- Blueprint Definition ---
main = Blueprint('main', __name__)


# =======================================================
# GEMINI CHAT SETUP (CRITICAL FOR DASHBOARD CHAT)
# =======================================================
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

chat_model_name = "gemini-2.5-flash"
chat_model = None

# Initialize model instance once
try:
    chat_model = genai.GenerativeModel(chat_model_name)
    current_app.logger.info(f"Gemini chat model '{chat_model_name}' initialized.")
except Exception as e:
    current_app.logger.error(f"Failed to initialize Gemini chat model: {e}")


# --- NEW HELPER: Serialization Function (ABSOLUTE FIX) ---
def serialize_history(history):
    """
    Converts a list of Gemini Content objects into a JSON-serializable list of dictionaries.
    This handles multiple SDK versions by explicitly accessing role/parts attributes.
    """
    serialized_messages = []
    for h in history:
        # Check if the object needs serialization (i.e., if it's a model Content object)
        if hasattr(h, 'role') and hasattr(h, 'parts'):
            # This is the safest way to serialize Content objects across SDK versions
            serialized_messages.append({
                "role": h.role,
                # Serialize parts, assuming parts are simple text objects or already serializable
                "parts": [p.to_dict() if hasattr(p, 'to_dict') else {"text": str(p)} for p in h.parts]
            })
        else:
            # Assume it is already a dictionary and safe to store
            serialized_messages.append(h)
            
    return serialized_messages

def get_chat_session(user_id, attempt_count=0):
    """Initializes or retrieves the persistent chat history for a given user ID from the session."""
    session_key = f'chat_history_{user_id}'
    
    if chat_model is None:
        return None
    
    if attempt_count > 1:
        # Prevent infinite recursion if chat initialization fails repeatedly
        current_app.logger.error("Chat initialization failed twice. Giving up on chat session.")
        return None

    if session_key not in session:
        # 1. NEW SESSION INITIALIZATION
        system_instruction = (
            "You are a helpful and supportive AI study assistant named Gemini. "
            "Keep your responses concise, academic, and supportive. "
            "Do not include markdown for code blocks unless absolutely necessary."
        )
        try:
            # Start a new chat session with a clean slate
            chat = chat_model.start_chat(history=[
                {"role": "user", "parts": [{"text": system_instruction}]},
                {"role": "model", "parts": [{"text": "Hello! I am Gemini, your AI study assistant. How can I help you explore knowledge today?"}]}
            ])
            # FIX 1: Serialize the history (list of dictionaries) before storing in session
            session[session_key] = serialize_history(chat.history) 
            return chat
        except Exception as e:
            current_app.logger.error(f"Failed to start initial Gemini chat session: {e}")
            return None
    
    # 2. REBUILDING SESSION FROM HISTORY
    try:
        # The history stored in the session is a serializable list of dictionaries, 
        # which start_chat correctly interprets when passed as the history argument.
        history = session[session_key]
        chat = chat_model.start_chat(history=history)
        return chat
    except Exception as e:
        # CRITICAL FIX for TypeError: If rebuilding from stored history fails, assume corruption and clear session.
        current_app.logger.error(f"Failed to rebuild Gemini chat session from history: {e}. Clearing session history.")
        # Clear the corrupted session entry
        del session[session_key]
        session.modified = True
        
        # Try recursion once to re-initialize from a clean slate
        return get_chat_session(user_id, attempt_count + 1)
        
# =======================================================
# END CHAT SETUP
# =======================================================


# --- Helper Function ---
def allowed_file(filename):
    """Checks if a file's extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config.get('ALLOWED_EXTENSIONS', set())


# --- CORE & TUTOR ROUTES ---

@main.route('/')
def home():
    """Homepage: Redirects to dashboard if logged in, otherwise to login page."""
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('main.login'))

@main.route('/tutor-index')
def tutor_index():
    """The main page for the multimodal tutor."""
    if 'user_id' not in session:
        flash("Please log in to access the tutor.", "warning")
        return redirect(url_for('main.login'))
    return render_template('index.html')

@main.route('/get_answer', methods=['POST'])
def get_answer():
    """Processes a query and returns a multimodal lesson page."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))

    query = request.form.get('query')
    if not query:
        flash("A question is required.", "danger")
        return redirect(url_for('main.tutor_index'))

    # Defensive calls with logging to avoid uncaught exceptions seen in terminal
    try:
        text_answer = text_generation.generate_text_answer(query)
    except Exception as e:
        current_app.logger.exception("Error generating text answer")
        text_answer = "Sorry, I couldn't generate an answer at this time."

    try:
        # NOTE: Assuming text_to_speech.generate_audio exists and returns a URL/filename
        audio_url = text_to_speech.generate_audio(text_answer)
    except Exception as e:
        current_app.logger.exception("Error generating audio")
        audio_url = None

    try:
        top_videos = video_search.find_top_videos(query, max_results=3) or []
    except Exception as e:
        current_app.logger.exception("Error fetching top videos")
        top_videos = []

    try:
        top_images = image_handling.find_relevant_images(query, max_results=4) or []
    except Exception as e:
        current_app.logger.exception("Error fetching images")
        top_images = []

    return render_template(
        'lesson.html', query=query, text_answer=text_answer,
        audio_url=audio_url, top_videos=top_videos, top_images=top_images
    )

# --- Authentication & User Profile Routes ---

@main.route('/register', methods=['GET', 'POST'])
def register():
    """Handles new user registration."""
    if request.method == 'POST':
        email = request.form.get('email')
        if User.query.filter_by(email=email).first():
            flash("Email already registered. Please log in.", "danger")
            return redirect(url_for('main.login'))

        new_user = User(
            first_name=request.form.get('first_name'),
            last_name=request.form.get('last_name'),
            dob=request.form.get('dob'),
            email=email,
            password=request.form.get('password')  # User model expected to hash via set_password or in setter
        )
        db.session.add(new_user)
        db.session.commit()
        flash("Registration successful! Please log in.", "success")
        return redirect(url_for('main.login'))
    return render_template('register.html')

@main.route('/login', methods=['GET', 'POST'])
def login():
    """Handles user login."""
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        user = User.query.filter_by(email=request.form.get('email')).first()

        if user and user.check_password(request.form.get('password')):
            session['user_id'] = user.id
            flash("Login successful!", "success")
            return redirect(url_for('main.welcome'))
        else:
            flash("Invalid email or password.", "danger")
    return render_template('login.html')

@main.route('/logout')
def logout():
    """Logs the user out."""
    session.clear()
    flash("You have been successfully logged out.", "info")
    return redirect(url_for('main.login'))

@main.route('/profile', methods=['GET', 'POST'])
def profile():
    """Displays and handles updates for the user profile."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))
    user = User.query.get_or_404(session['user_id'])

    if request.method == 'POST':
        user.first_name = request.form.get('first_name')
        user.last_name = request.form.get('last_name')
        user.dob = request.form.get('dob')

        if 'profile_pic' in request.files:
            pic = request.files['profile_pic']
            if pic and pic.filename and allowed_file(pic.filename):
                filename = secure_filename(f"{user.id}_{pic.filename}")
                upload_folder = current_app.config.get('UPLOAD_FOLDER')
                if upload_folder:
                    pic.save(os.path.join(upload_folder, filename))
                    user.profile_pic = filename
                else:
                    current_app.logger.warning("UPLOAD_FOLDER is not configured; skipping save.")

        db.session.commit()
        flash("Profile updated successfully!", "success")
        return redirect(url_for('main.profile'))
    return render_template('profile.html', user=user)

@main.route('/change_password', methods=['POST'])
def change_password():
    """Handles changing a user's password from the profile page."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))
    user = User.query.get_or_404(session['user_id'])

    current = request.form.get('current_password')
    new = request.form.get('new_password')
    confirm = request.form.get('confirm_password')

    if not user.check_password(current):
        flash("Current password is incorrect.", "danger")
    elif new != confirm:
        flash("New passwords do not match.", "danger")
    else:
        user.set_password(new)
        db.session.commit()
        flash("Password changed successfully!", "success")

    return redirect(url_for('main.profile'))

# --- AI Chatbot & Generator Routes ---


@main.route('/chat_page')
def chat_page():
    """Render standalone chat interface page."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))
    return render_template('chat.html')


@main.route('/chat', methods=['POST'])
def chat():
    """Handle user chat input and return Gemini response."""
    if 'user_id' not in session:
        return jsonify({"reply": "Authentication required."}), 401

    try:
        user_input = request.json.get("message")
        if not user_input:
            return jsonify({"reply": "Please enter a message."})

        if chat_model is None:
            return jsonify({"reply": "Chat model not initialized."}), 500

        # Start a new chat session for this request
        chat_session = chat_model.start_chat()
        response = chat_session.send_message(user_input)

        # Return text if available
        reply_text = getattr(response, "text", None)
        if reply_text:
            return jsonify({"reply": reply_text})
        else:
            return jsonify({"reply": "Hmm... I couldn't get a proper response from Gemini."})

    except Exception as e:
        current_app.logger.exception("Error in chat route")
        return jsonify({"reply": f"Error: {str(e)}"}), 500
# =======================================================
# QUIZ GENERATOR ROUTES
# =======================================================

@main.route('/generate-quiz')
def generate_quiz_page():
    """Renders the dedicated AI quiz generator interface page (quiz.html)."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))
    return render_template('quiz.html')


@main.route('/generate_quiz_api', methods=['POST'])
def generate_quiz_api():
    """API endpoint to generate a quiz from a user topic using the Gemini model."""
    if 'user_id' not in session:
        return jsonify({"error": "Authentication required."}), 401

    if not request.is_json:
        return jsonify({"error": "Missing JSON in request"}), 400

    data = request.get_json()
    topic = data.get('topic')

    if not topic:
        return jsonify({"error": "Missing 'topic' in request."}), 400

    current_app.logger.info(f"Generating quiz for topic: {topic}")

    try:
        if not callable(generate_quiz_func):
            current_app.logger.error("Quiz generation function not found in modules.text_generation")
            return jsonify({"error": "Quiz generation not available."}), 500

        quiz_data = generate_quiz_func(topic)
    except Exception as e:
        current_app.logger.exception("Quiz generation failed")
        return jsonify({"error": "Quiz generation failed."}), 500

    if isinstance(quiz_data, dict) and 'error' in quiz_data:
        current_app.logger.error(f"Quiz Generation Failed: {quiz_data['error']}")
        return jsonify(quiz_data), 500

    return jsonify(quiz_data)

@main.route('/save_quiz_results', methods=['POST'])
def save_quiz_results():
    """Receives quiz results from frontend and saves them to the database."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))

    if not request.is_json:
        return jsonify({"error": "Missing JSON in request"}), 400

    user_id = session['user_id']
    data = request.get_json()

    topic = data.get('topic')
    score = data.get('score')
    total = data.get('total')
    detail = data.get('detail')

    if None in [topic, score, total, detail]:
        return jsonify({"error": "Missing required quiz result data."}), 400

    try:
        score = int(score)
        total = int(total)
        percentage = (score / total) * 100 if total > 0 else 0

        new_history = QuizHistory(
            user_id=user_id,
            topic=topic,
            score=score,
            total_questions=total,
            percentage=percentage,
            results_detail=json.dumps(detail)
        )

        db.session.add(new_history)
        db.session.commit()

        return jsonify({"success": True, "message": "Quiz results saved successfully."}), 200

    except Exception as e:
        current_app.logger.exception("Error saving quiz results")
        db.session.rollback()
        return jsonify({"success": False, "error": "Failed to save results."}), 500


@main.route('/quiz-history')
def quiz_history():
    """Renders a page showing the user's past AI quiz scores."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))

    user_id = session['user_id']
    history = QuizHistory.query.filter_by(user_id=user_id).order_by(QuizHistory.date_taken.desc()).all()

    return render_template('quiz_history.html', history=history)

# =======================================================
# END QUIZ GENERATOR ROUTES
# =======================================================


# --- Learning Hub & Dashboard Routes ---

@main.route('/dashboard')
def dashboard():
    """Displays the user's dashboard with course progress."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('main.login'))

    # NOTE: user ID must be convertible to int or whatever your DB uses, 
    # but query.get_or_404 is typically safe if the session key matches the primary key type.
    user = User.query.get_or_404(user_id)
    
    # === CRUCIAL STEP: Initialize Chat Session ===
    # This ensures the chat history is ready when the page loads.
    get_chat_session(user_id) 
    # ============================================

    all_courses = Course.query.all()

    progress_data = []
    for course in all_courses:
        
        total_videos = len(getattr(course, 'videos', []))
        completed_count = 0 
        # Ensure user.completed_videos is fetched safely, assuming a backref or many-to-many relationship
        user_completed_videos = getattr(user, 'completed_videos', []) 

        if total_videos > 0:
            # Calculate completed count based on videos present in the user's completed list
            completed_count = sum(1 for video in course.videos if video in user_completed_videos)
            percentage = int((completed_count / total_videos) * 100)
            
            if percentage == 100:
                status = "Completed"
            elif percentage > 0:
                status = "In Progress"
            else:
                status = "Not Started"
        else:
            status = "No Content"
            percentage = 0

        progress_data.append({
            'course': course,
            'status': status,
            'percentage': percentage
        })

    return render_template('dashboard.html', user=user, progress_data=progress_data)

@main.route('/courses')
def courses():
    """Lists all available courses."""
    all_courses = Course.query.all()
    return render_template('courses.html', courses=all_courses)

@main.route('/course/<int:course_id>')
def course_detail(course_id):
    """Shows details, videos, and quizzes for a specific course."""
    course = Course.query.get_or_404(course_id)
    return render_template('course_detail.html', course=course)

@main.route('/video/<int:video_id>')
def video_detail(video_id):
    """Shows a specific course video."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))
    video = Video.query.get_or_404(video_id)
    user = User.query.get_or_404(session['user_id'])
    is_completed = video in getattr(user, 'completed_videos', [])
    return render_template('video_detail.html', video=video, is_completed=is_completed)

@main.route('/complete_video/<int:video_id>', methods=['POST'])
def complete_video(video_id):
    """Marks a video as complete for the user."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))
    video = Video.query.get_or_404(video_id)
    user = User.query.get_or_404(session['user_id'])

    if video not in getattr(user, 'completed_videos', []):
        # NOTE: Assuming user.completed_videos is a list-like object for relationship management
        user.completed_videos.append(video) 
        db.session.commit()
        flash(f"Completed: {video.title}", "success")

    return redirect(url_for('main.course_detail', course_id=video.course_id))

@main.route('/welcome')
def welcome():
    """Shows a temporary welcome splash screen."""
    if 'user_id' not in session:
        return redirect(url_for('main.login'))

    return render_template('welcome.html')


@main.route('/generate_notes_api', methods=['POST'])
def generate_notes_api():
    """
    Generates study notes via AI, saves the document, and returns the preview data/download link.
    """
    if 'user_id' not in session:
        return jsonify({"error": "Authentication required."}), 401

    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    topic = None
    doc_format = 'docx'
    markdown_content = "" 

    try:
        data = request.get_json()
        topic = data.get('topic')
        doc_format = data.get('format', 'docx')

        if not topic:
            return jsonify({"error": "Missing 'topic' in request."}), 400

        logging.info(f"Starting complete note generation for topic: {topic}, format: {doc_format}")
        
        if not hasattr(text_generation, 'generate_complete_notes'):
            return jsonify({"error": "AI generation function is not available on the server."}), 500

        # 1. Generate Structured Notes (Markdown)
        ai_result = text_generation.generate_complete_notes(topic)
        
        if not ai_result.get('success'):
            return jsonify({"error": ai_result.get('error', 'AI returned error.')}), 500
        
        markdown_content = ai_result.get("content_markdown")
        if not markdown_content or not isinstance(markdown_content, str):
            current_app.logger.error("Invalid markdown content from AI")
            return jsonify({
        "error": "AI failed to generate valid notes."
    }), 500
        
        # 2. Generate and Save the Document 
        file_path_relative = document_generator.create_and_save_document(
            topic,
            markdown_content,
            format='docx' # NOTE: Hardcoded to DOCX for robust output
        )
        
        if file_path_relative.startswith("Error:"):
            logging.error(f"Document generator reported error: {file_path_relative}")
            return jsonify({"error": file_path_relative}), 500

        # 3. Create absolute download link and final filename
        download_url = url_for('static', filename=file_path_relative.replace('static/', ''), _external=True)
        filename = document_generator.get_file_name(topic, 'docx')

        return jsonify({
            "success": True,
            "title": topic,
            "filename": filename,
            "download_url": download_url,
            "content_markdown": markdown_content # RETURN RAW CONTENT FOR PREVIEW
        }), 200

    except Exception as e:
        current_app.logger.exception(f"Unhandled error in generate_notes_api for topic: {topic}")
        return jsonify({"error": "An unexpected server error occurred."}), 500