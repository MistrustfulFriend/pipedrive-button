from flask import Flask, request, jsonify, send_file, session
from flask_cors import CORS
import openai
import os
from datetime import datetime, timedelta
import random
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
import secrets

load_dotenv()

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Secret key for sessions
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True in production with HTTPS
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

openai.api_key = os.environ.get('OPENAI_API_KEY')

# MongoDB Setup
MONGODB_URI = os.environ.get('MONGODB_URI')
if not MONGODB_URI:
    print("WARNING: MONGODB_URI environment variable is not set!")
else:
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        db = client['german_learning']
        users_collection = db['users']
        dictionary_collection = db['dictionary']
        log_collection = db['learning_log']
        print("✓ MongoDB connected successfully")
    except Exception as e:
        print(f"MongoDB connection error: {e}")
        client = None
        db = None

if not openai.api_key:
    print("WARNING: OPENAI_API_KEY environment variable is not set!")

# Exercise history to avoid repetition
exercise_history = []
MAX_HISTORY = 20

TOPIC_DESCRIPTIONS = {
    'conv_greetings': 'basic greetings, introductions, and polite phrases',
    'conv_food': 'food items, dining, ordering at restaurants',
    'conv_travel': 'travel vocabulary, directions, transportation',
    'conv_shopping': 'shopping, prices, clothing, stores, money',
    'conv_work': 'work, business, professional communication',
    'conv_hobbies': 'hobbies, leisure activities, sports',
    'conv_family': 'family members, relationships, personal life',
    'conv_health': 'health, body parts, medical situations',
    'conv_weather': 'weather conditions, nature, seasons',
    'conv_education': 'education, learning, school, university',
    'conv_technology': 'technology, media, internet, computers',
    'conv_culture': 'culture, traditions, customs, celebrations',
    'conv_housing': 'housing, apartments, living situations',
    'conv_emergency': 'emergency situations, urgent communication',
    'conv_entertainment': 'entertainment, events, concerts, theater',
    'conv_opinions': 'expressing opinions, agreeing, disagreeing',
    'conv_smalltalk': 'small talk, chitchat, casual conversation',
    'conv_complaints': 'complaints, problems, dissatisfaction',
    'gram_cases': 'German cases (Nominativ, Akkusativ, Dativ, Genitiv)',
    'gram_articles': 'German articles (der, die, das)',
    'gram_verbs': 'verb conjugation in German',
    'gram_tenses': 'German tenses (present, past, future)',
    'gram_word_order': 'German word order and sentence structure',
    'gram_prepositions': 'German prepositions and their cases',
    'gram_adjectives': 'adjective endings in German',
    'gram_modal': 'German modal verbs (können, müssen, etc.)',
    'gram_pronouns': 'pronouns (personal, possessive, demonstrative)',
    'gram_reflexive': 'reflexive verbs in German',
    'gram_separable': 'separable and inseparable verbs',
    'gram_passive': 'passive voice construction',
    'gram_subjunctive': 'subjunctive mood (Konjunktiv I & II)',
    'gram_imperatives': 'imperatives and commands',
    'gram_comparatives': 'comparatives and superlatives',
    'gram_conjunctions': 'conjunctions and connectors',
    'gram_relative': 'relative clauses',
    'gram_infinitive': 'infinitive constructions (zu + infinitive)',
    'vocab_verbs': 'common German verbs',
    'vocab_nouns': 'common German nouns with articles',
    'vocab_adjectives': 'German adjectives and adverbs',
    'vocab_phrases': 'useful German phrases and idioms',
    'vocab_numbers': 'German numbers and time expressions',
    'vocab_colors': 'German colors and descriptions',
    'vocab_emotions': 'emotions and feelings vocabulary',
    'vocab_animals': 'animals and pets',
    'vocab_clothing': 'clothing and fashion vocabulary',
    'vocab_transport': 'transportation vocabulary',
    'vocab_professions': 'professions and jobs',
    'vocab_kitchen': 'kitchen and cooking vocabulary',
    'vocab_sports': 'sports and fitness vocabulary',
    'vocab_office': 'office and business vocabulary'
}

CREATIVE_SCENARIOS = [
    "You're at a German farmers market and discover an unusual vegetable",
    "You accidentally joined a German book club meeting",
    "You're teaching your German neighbor how to make your favorite dish",
    "You found a mysterious letter in German in an old book",
    "You're helping organize a surprise party for a German friend",
    "You're at a German flea market negotiating for a vintage item",
    "You met a German time traveler from 1920",
    "You're explaining your unusual hobby to curious Germans",
    "You're stuck in an elevator with Germans and making small talk",
    "You're collaborating with Germans on a quirky art project"
]

CREATIVE_WRITING_PROMPTS = [
    "You discovered a magic portal in your local library",
    "You woke up speaking only German in a parallel universe",
    "You're writing a letter to your future self 10 years from now",
    "You found a mysterious package with your name on it",
    "You can communicate with animals for one day",
    "You're the last person on Earth who remembers yesterday",
    "You inherited a peculiar object from a distant relative",
    "You can see 5 minutes into the future"
]

# ==================== AUTHENTICATION MIDDLEWARE ====================

def login_required(f):
    """Decorator to require login for routes"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required", "auth_required": True}), 401
        return f(*args, **kwargs)
    return decorated_function

def get_current_user_id():
    """Get current logged-in user ID"""
    return session.get('user_id')

# ==================== AUTHENTICATION ROUTES ====================

@app.route('/api/auth/register', methods=['POST'])
def register():
    """Register a new user"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        data = request.json
        username = data.get('username', '').strip().lower()
        password = data.get('password', '')
        email = data.get('email', '').strip().lower()
        
        if not username or len(username) < 3:
            return jsonify({"error": "Username must be at least 3 characters"}), 400
        
        if not password or len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        
        if users_collection.find_one({'username': username}):
            return jsonify({"error": "Username already exists"}), 400
        
        if email and users_collection.find_one({'email': email}):
            return jsonify({"error": "Email already registered"}), 400
        
        user = {
            'username': username,
            'email': email,
            'password_hash': generate_password_hash(password),
            'created_at': datetime.now().isoformat(),
            'last_login': None
        }
        
        result = users_collection.insert_one(user)
        user_id = str(result.inserted_id)
        
        session.permanent = True
        session['user_id'] = user_id
        session['username'] = username
        
        return jsonify({
            "success": True,
            "user": {
                "id": user_id,
                "username": username,
                "email": email
            }
        }), 201
        
    except Exception as e:
        print(f"Registration error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    """Login user"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        data = request.json
        username = data.get('username', '').strip().lower()
        password = data.get('password', '')
        
        if not username or not password:
            return jsonify({"error": "Username and password required"}), 400
        
        user = users_collection.find_one({'username': username})
        
        if not user or not check_password_hash(user['password_hash'], password):
            return jsonify({"error": "Invalid username or password"}), 401
        
        users_collection.update_one(
            {'_id': user['_id']},
            {'$set': {'last_login': datetime.now().isoformat()}}
        )
        
        session.permanent = True
        session['user_id'] = str(user['_id'])
        session['username'] = user['username']
        
        return jsonify({
            "success": True,
            "user": {
                "id": str(user['_id']),
                "username": user['username'],
                "email": user.get('email', '')
            }
        }), 200
        
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Logout user"""
    session.clear()
    return jsonify({"success": True}), 200

@app.route('/api/auth/me', methods=['GET'])
@login_required
def get_current_user():
    """Get current user info"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        user_id = get_current_user_id()
        user = users_collection.find_one({'_id': ObjectId(user_id)})
        
        if not user:
            session.clear()
            return jsonify({"error": "User not found"}), 404
        
        return jsonify({
            "user": {
                "id": str(user['_id']),
                "username": user['username'],
                "email": user.get('email', ''),
                "created_at": user.get('created_at')
            }
        }), 200
        
    except Exception as e:
        print(f"Get user error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/check', methods=['GET'])
def check_auth():
    """Check if user is authenticated"""
    if 'user_id' in session:
        return jsonify({
            "authenticated": True,
            "username": session.get('username')
        }), 200
    else:
        return jsonify({"authenticated": False}), 200

# ==================== STATIC FILE ROUTES - KEEP ONLY ONE SET ====================

@app.route('/')
def home():
    return send_file('index.html')

@app.route('/login.html')
def login_page():
    return send_file('login.html')

@app.route('/style.css')
def serve_css():
    return send_file('style.css')

@app.route('/script.js')
def serve_js():
    return send_file('script.js')
# ==================== EXERCISE GENERATION (keeping existing functions) ====================

def generate_exercise(topics, exercise_type, dictionary_words=None):
    """Generate creative, non-repetitive exercises with improved prompts"""
    
    if not openai.api_key:
        return "Error: OpenAI API key not configured."
    
    # Build topic context
    topic_descriptions = []
    custom_text = None
    
    for t in topics:
        if t.get('value') == 'custom_practice':
            custom_text = t.get('text', '')
        else:
            topic_descriptions.append(TOPIC_DESCRIPTIONS.get(t['value'], t['value']))
    
    # Determine topic context
    if custom_text:
        topic_context = f"Custom topic: {custom_text}"
    elif topic_descriptions:
        topic_context = ", ".join(topic_descriptions)
    else:
        topic_context = "general German practice"
    
    # Build history context
    history_context = ""
    if exercise_history:
        recent = exercise_history[-5:]
        history_context = f"\n\nPrevious exercises to avoid repeating:\n" + "\n".join([f"- {ex}" for ex in recent])
    
    # Dictionary words context
    dict_context = ""
    if dictionary_words and len(dictionary_words) > 0:
        words_list = [f"{w['german']} ({w['english']})" for w in dictionary_words[:5]]
        dict_context = f"\n\nMust include these words: {', '.join(words_list)}"
    
    prompts = {
        'translation': f"""Create a German-to-English translation exercise.

TOPIC: {topic_context}
{dict_context}
{history_context}

REQUIREMENTS:
- Level: A1-C2 (intermediate complexity preferred)
- Create ONE complete German sentence to translate
- Sentence should be natural and contextually rich
- Include cultural context or idiomatic expressions when relevant
- Avoid clichés like "I go to the store" or "The weather is nice"

OUTPUT FORMAT (exact structure):
Translate this German sentence to English:
[Your German sentence here]

IMPORTANT: 
- Output ONLY the task in the format above
- Do NOT include English translation
- Do NOT add explanations, tips, or additional context
- Do NOT number the task""",
        
        'conversation': f"""Create a conversational German exercise.

TOPIC: {topic_context}
SCENARIO: {random.choice(CREATIVE_SCENARIOS)}
{dict_context}
{history_context}

REQUIREMENTS:
- Level: A1-C2 (intermediate complexity preferred)
- Create a realistic situation and a prompt requiring a German response
- Use natural conversational language, not textbook phrases
- Make the scenario engaging and memorable

OUTPUT FORMAT (exact structure):
Situation: [2-3 sentence scenario description]
Respond in German to: [Specific prompt or question]

IMPORTANT:
- Output ONLY the task in the format above
- Do NOT include the English translation of the expected answer
- Do NOT include sample responses or hints
- Do NOT add explanations beyond the situation description""",
        
        'grammar': f"""Create a German grammar exercise.

TOPIC: {topic_context}
{dict_context}
{history_context}

REQUIREMENTS:
- Level: A1-C2 (intermediate complexity preferred)
- Focus on ONE specific grammar concept
- Create 2-3 sentences testing this concept
- Use engaging, memorable examples with context
- Include clear instructions on what to do

OUTPUT FORMAT:
[Clear instruction about what grammar to practice]
[2-3 example sentences with blanks or items to correct]

IMPORTANT:
- Output ONLY the task with clear instructions
- Do NOT include the answers
- Do NOT add explanatory notes or grammar rules
- Make instructions specific (e.g., "Fill in the correct article" not "Practice articles")""",
        
        'vocabulary': f"""Create a German vocabulary exercise.

TOPIC: {topic_context}
{dict_context}
{history_context}

REQUIREMENTS:
- Present exactly 3-4 German words or phrases
- Include interesting, useful expressions (can include some lesser-known ones)
- For each word provide: German word, English meaning, and one example sentence
- Add brief cultural context if relevant

OUTPUT FORMAT:
Learn these German words:

1. [German word/phrase] - [English meaning]
   Example: [German sentence using the word]
   [Optional: Brief cultural note]

2. [Next word...]

IMPORTANT:
- Output ONLY the vocabulary list in the format above
- Keep cultural notes brief (one sentence max)
- Use natural, authentic German in examples""",
        
        'dictionary_practice': f"""Create an exercise using the user's dictionary words.

WORDS TO PRACTICE:
{dict_context}
{history_context}

REQUIREMENTS:
- Create ONE of these exercise types:
  a) A paragraph with blanks to fill using the dictionary words
  b) Sentences to translate that naturally include the words
  c) A short dialogue using the words
- Make the context natural and interesting
- Level: A1-C2 (match the complexity of the words)

OUTPUT FORMAT:
[Clear instruction]
[Exercise content]

IMPORTANT:
- Output ONLY the task
- Do NOT include answers
- Do NOT add vocabulary definitions (user already knows these words)
- Ensure all dictionary words are genuinely needed for the exercise""",

        'listening_practice': f"""Create a German listening comprehension exercise.

TOPIC: {topic_context}
{dict_context}
{history_context}

REQUIREMENTS:
- Level: A1-C2 (intermediate complexity preferred)
- Create a short dialogue or monologue (3-5 sentences) in German
- Include authentic conversational elements (fillers, contractions, colloquialisms)
- Provide 2-3 comprehension questions in English
- Questions should test understanding of main ideas, details, or implied meaning

OUTPUT FORMAT (exact structure):
Listen to this German text:
[German dialogue or monologue here - 3-5 sentences]

Answer these questions:
1. [Question in English]
2. [Question in English]
3. [Question in English]

IMPORTANT:
- Output ONLY the task in the format above
- Do NOT include answers to the questions
- Do NOT provide translations of the German text
- Use natural, conversational German with realistic speech patterns
- Questions should encourage active listening and comprehension""",

        'creative_writing': f"""Create a German creative writing exercise.

TOPIC: {topic_context}
CREATIVE PROMPT: {random.choice(CREATIVE_WRITING_PROMPTS)}
{dict_context}
{history_context}

REQUIREMENTS:
- Level: A2-C2 (encourage creative expression)
- Provide an engaging creative prompt or story starter
- Ask for a 5-8 sentence response in German
- Encourage use of specific grammar structures or vocabulary
- Make it fun and imaginative

OUTPUT FORMAT (exact structure):
Creative Writing Challenge:
[Engaging scenario or prompt - 2-3 sentences]

Write 5-8 sentences in German about:
[Specific writing task]

Try to include: [2-3 grammar or vocabulary suggestions]

IMPORTANT:
- Output ONLY the task in the format above
- Do NOT provide a sample response
- Do NOT include translations
- Make prompts imaginative and engaging
- Encourage personal expression and creativity""",

        'error_correction': f"""Create a German error detection and correction exercise.

TOPIC: {topic_context}
{dict_context}
{history_context}

REQUIREMENTS:
- Level: A2-C2 (intermediate to advanced)
- Create 3-4 German sentences with deliberate mistakes
- Include variety of error types: grammar (cases, verb conjugation, word order), vocabulary misuse, article errors, preposition errors
- Errors should be realistic (common learner mistakes)
- Make sentences contextually connected (tell a mini-story)

OUTPUT FORMAT (exact structure):
Error Detective Challenge:
Find and correct the mistakes in these German sentences:

1. [German sentence with error]
2. [German sentence with error]
3. [German sentence with error]
4. [German sentence with error - optional]

Hint: Look for errors in [general hints like "articles, verb conjugation, and word order"]

IMPORTANT:
- Output ONLY the task in the format above
- Do NOT mark where the errors are
- Do NOT provide the corrected versions
- Do NOT explain the errors
- Errors should be realistic and educational
- Sentences should form a coherent context or mini-story"""
    }
    
    # Select appropriate prompt
    if dictionary_words and len(dictionary_words) > 0:
        selected_prompt = prompts.get('dictionary_practice', prompts['translation'])
    else:
        selected_prompt = prompts.get(exercise_type, prompts['translation'])
    
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system", 
                    "content": "You are a German language exercise creator. Generate exercises EXACTLY as specified in the format provided. Do not add extra explanations, answers, or formatting beyond what is requested. Be creative with content but strict with format."
                },
                {"role": "user", "content": selected_prompt}
            ],
            max_tokens=350,
            temperature=0.8
        )
        
        question = response.choices[0].message.content.strip()
        
        # Store in history
        exercise_history.append(question[:100])
        if len(exercise_history) > MAX_HISTORY:
            exercise_history.pop(0)
        
        return question
        
    except Exception as e:
        print(f"Error generating exercise: {e}")
        return f"Error generating exercise: {str(e)}"


def check_answer(question, answer, exercise_type):
    """Check answers with structured, helpful feedback"""
    
    if not openai.api_key:
        return "Error: OpenAI API key not configured."
    
    prompts = {
        'translation': f"""Evaluate this German translation exercise.

EXERCISE: {question}
STUDENT'S ANSWER: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Excellent/Good/Needs Improvement]

2. EVALUATION:
   - What is correct: [Be specific]
   - What needs correction: [If any errors exist]
   - Correct answer: [Only if student's answer was incorrect]

3. EXPLANATION: [Why the correct answer works, brief grammar/vocabulary notes]

4. TIP: [One helpful mnemonic or learning tip]

Be encouraging but honest. Focus on learning, not just praise.""",
            
        'conversation': f"""Evaluate this German conversation response.

SCENARIO: {question}
STUDENT'S RESPONSE: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Excellent/Good/Needs Improvement]

2. EVALUATION:
   - Appropriateness: [Is the response culturally and contextually appropriate?]
   - Grammar: [Identify any errors and correct them]
   - Vocabulary: [Comment on word choice]
   - Naturalness: [Does it sound like natural German?]

3. NATIVE ALTERNATIVE: [Suggest how a native speaker might say this]

4. TIP: [One practical improvement for future responses]

Be constructive and supportive.""",
        
        'grammar': f"""Evaluate this German grammar exercise.

EXERCISE: {question}
STUDENT'S ANSWER: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Excellent/Good/Needs Improvement]

2. ANALYSIS:
   - Correct elements: [What they got right]
   - Errors: [What needs correction, if any]
   - Correct answer: [Provide if incorrect]

3. GRAMMAR EXPLANATION: [Explain the rule briefly and clearly]

4. MEMORY TRICK: [Provide a helpful mnemonic or pattern to remember]

Be clear and educational.""",
        
        'vocabulary': f"""Evaluate this German vocabulary exercise.

EXERCISE: {question}
STUDENT'S ANSWERS: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Score like "3/4 correct" or overall evaluation]

2. REVIEW:
   - Correct answers: [List them with praise]
   - Incorrect answers: [Show correct form with explanation]

3. ADDITIONAL INFO: [Related words, usage notes, or cultural context]

4. TIP: [Memory technique or learning suggestion]

Be encouraging and informative.""",

        'listening_practice': f"""Evaluate answers to this German listening comprehension exercise.

EXERCISE: {question}
STUDENT'S ANSWERS: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Score like "2/3 correct" or overall evaluation]

2. ANSWER REVIEW:
   - Question 1: [Correct/Incorrect - brief explanation]
   - Question 2: [Correct/Incorrect - brief explanation]
   - Question 3: [Correct/Incorrect - brief explanation]

3. COMPREHENSION ANALYSIS:
   - What the student understood well
   - What was missed or misunderstood
   - Key vocabulary or phrases that were important

4. LISTENING TIP: [Specific advice for improving German listening skills]

Be encouraging and focus on comprehension strategies.""",

        'creative_writing': f"""Evaluate this German creative writing exercise.

EXERCISE: {question}
STUDENT'S WRITING: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Excellent/Good/Needs Improvement]

2. CONTENT & CREATIVITY:
   - How well the prompt was addressed
   - Creativity and originality of ideas
   - Engagement and interest level

3. LANGUAGE QUALITY:
   - Grammar accuracy: [Note any errors]
   - Vocabulary usage: [Richness, appropriateness]
   - Sentence structure: [Variety, complexity]
   - Natural flow: [Does it sound natural?]

4. CORRECTIONS: [List any grammar/vocabulary errors with corrections]

5. SUGGESTIONS: [2-3 specific ways to enhance the writing]

6. ENCOURAGEMENT: [Positive note about what worked well]

Be supportive and constructive. Focus on both content and language.""",

        'error_correction': f"""Evaluate this German error correction exercise.

ORIGINAL EXERCISE: {question}
STUDENT'S CORRECTIONS: {answer}

REQUIREMENTS:
Provide feedback in English with this structure:

1. ASSESSMENT: [Score like "3/4 errors found and corrected"]

2. ERROR-BY-ERROR REVIEW:
   Sentence 1: [Whether they found/corrected the error + correct version]
   Sentence 2: [Whether they found/corrected the error + correct version]
   Sentence 3: [Whether they found/corrected the error + correct version]
   Sentence 4: [Whether they found/corrected the error + correct version - if applicable]

3. EXPLANATIONS:
   - Explain each error type (why it was wrong)
   - Provide the grammar rule or principle
   - Mention if they missed any errors

4. LEARNING POINT: [Key takeaway about common German mistakes]

Be clear and educational. Help them understand WHY errors occur."""
    }
    
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system", 
                    "content": "You are a German language teacher providing structured, helpful feedback. Follow the format exactly. Be supportive but accurate—don't say something is correct if it isn't. Provide clear explanations that help students understand and improve."
                },
                {"role": "user", "content": prompts.get(exercise_type, prompts['translation'])}
            ],
            max_tokens=450,
            temperature=0.7
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        print(f"Error checking answer: {e}")
        return f"Error checking answer: {str(e)}"


def analyze_word(word, context):
    """Analyze a word with detailed information"""
    
    if not openai.api_key:
        return {
            'german': word,
            'english': 'API key not configured',
            'russian': 'Ключ API не настроен',
            'type': 'other',
            'category': 'vocabulary',
            'explanation': 'Please set OPENAI_API_KEY',
            'examples': []
        }
    
    prompt = f"""
        Analyze ONLY the following German word or phrase – do NOT analyze or include any other word from the context, unless it is part of the selected word itself.
       
         Your highest rule: 
        → If the word is a verb in any conjugated, participle, or modal form, 
        you MUST replace it with its infinitive form in the [German:] line.
        Do not ever keep the conjugated form (e.g., 'konnte', 'hatte', 'ging', 'schwimmt').
        The [German:] field must always contain the infinitive form (e.g., 'können', 'haben', 'gehen', 'schwimmen').

        If the word is a noun – use singular with article and plural in parentheses (e.g., der Tisch (die Tische)).
        If the word is an adjective – use base form (e.g., schön).
        If the word is an adverb – use base form (e.g., gern).
        If it's a participle used adjectivally – use adjective base (e.g., gefragt → gefragt).
        Never automatically create a noun from a verb or adjective.
        Analyze only the provided word, ignore others in the sentence.

        Word/Phrase: "{word}"
        Context (for reference only): {context}

        Provide the information in this EXACT format (each field on a new line):

        German: [see detailed type rules below]
        English: [English translation – accurate and specific]
        Russian: [Russian translation in Cyrillic – natural and precise]
        Type: [verb / noun / adjective / adverb / phrase / other]
        Category: [conversation / grammar / vocabulary]
        Explanation: [2–3 sentences – see requirements below]
        Example1: [Complete sentence in German] – [English translation]
        Example2: [Complete sentence in German] – [English translation]
        Example3: [Complete sentence in German] – [English translation]

        ---

        ### DETAILED RULES FOR "German" FIELD

        [1) IF IT IS A NOUN:]
        - Format: article + singular (plural), e.g., *das Haus (die Häuser)*, *der Tisch (die Tische)*.
        - Always keep nouns as nouns.
        - Normalize plural and declined forms.
        - Never convert nouns into verbs, adjectives, or adverbs.
        - Only create a noun from a verb/adjective root if the input is **capitalized** and used nominally (e.g., *das Gesagte*).
        - Ensure gender and plural forms are correct and natural.

        [2) IF IT IS A VERB:]
        - Always use infinitive form, e.g., *gehen*, *sprechen*, *haben*, *sein*, *können*.
        - Convert conjugated and participial forms:
        - *hatte / gehabt → haben*
        - *war / gewesen → sein*
        - *konnte / gekonnt → können*
        - *brachte / gebracht → bringen*
        - *ging / gegangen → gehen*
        - If it is reflexive → include "sich" + governed case, e.g., *sich erinnern an + Akkusativ*.
        - If it has a preposition → show infinitive + preposition + case, e.g., *warten auf + Akkusativ*.
        - If it is separable → show infinitive (present separation), e.g., *ankommen (kommt an)*.
        - Never create nouns automatically from verbs (e.g., *der Sprecher*, *das Gefragt*).

        [3) IF IT IS A PAST PARTICIPLE OR PARTICIPLE-FORM:]
        - Default: map to infinitive (e.g., *gefragt → fragen*, *getroffen → treffen*).
        - Treat as adjective only if context clearly shows adjectival use (*das gefragte Buch → gefragt*).
        - Treat as noun only if capitalized and clearly nominal (*das Gesagte*).

        [4) IF IT IS AN ADJECTIVE:]
        - Use base form, e.g., *schön*, *gut*.
        - Normalize comparative/superlative (*schöner*, *am schönsten → schön*).
        - Treat adjectival participles as adjectives derived from verbs (e.g., *sprechender Mann → sprechend*).
        - Create noun only if explicitly nominalized (*das Gesagte*).

        [5) IF IT IS AN ADVERB:]
        - Keep as adverb, e.g., *gern*, *heute*.
        - Do not convert into adjectives, verbs, or nouns.

        [6) IF IT IS A PHRASE / EXPRESSION:]
        - Keep base or infinitive form, indicate governed case if applicable.
        - Examples: *Lust haben auf + Akkusativ*, *es gibt + Akkusativ*.
        - Do not reduce phrase to a single word.

        ---

        ### GENERAL RULES (ALWAYS APPLY)
        - Always return verbs in **infinitive** form.
        - Always return nouns in **singular with plural in parentheses**.
        - Never reproduce the conjugated or declined form from the sentence.
        - Never analyze or include other words (e.g., nouns like "Socke") unless part of the same phrase.
        - Context is for meaning only, not for morphology.
        - Analyze ONLY the provided word. Ignore other nouns, verbs, or words in the sentence.
        - Preserve original part of speech; never switch automatically.
        - Normalize verbs → infinitive, adjectives → base, adverbs → base, nouns → singular (with plural).
        - Always ensure gender, plural, separability, and case governance are correct.
        - For ambiguous inputs, assume the most common POS and add note:
        "POS ambiguous – defaulted to [type]. Provide a sentence to override."

        ---

        ### EXPLANATION FIELD FORMAT
        [2–3 sentences covering:]
        - Primary meaning and usage.
        - Grammar details (gender, separability, case).
        - Common mistakes, nuances, or contextual tips.

        ---

        ### FORMATTING RULES
        - Always include articles for nouns.
        - Always show plural in parentheses for nouns.
        - Always specify case for prepositional or reflexive verbs.
        - Use " – " (em dash with spaces) between German and English in examples.
        - Do NOT add other words from the sentence.
        - Be concise, grammatical, and consistent.

        """
    
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a German language expert providing precise, well-formatted analysis."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500,
            temperature=0.3
        )
        
        content = response.choices[0].message.content.strip()
        print(f"Analyzing '{word}':\n{content}\n")
        
        word_data = {
            'german': '',
            'english': '',
            'russian': '',
            'type': 'other',
            'category': 'vocabulary',
            'explanation': '',
            'examples': []
        }
        
        lines = content.split('\n')
        examples = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            line_lower = line.lower()
            
            if line_lower.startswith('german:'):
                word_data['german'] = line.split(':', 1)[1].strip()
            elif line_lower.startswith('english:'):
                word_data['english'] = line.split(':', 1)[1].strip()
            elif line_lower.startswith('russian:'):
                word_data['russian'] = line.split(':', 1)[1].strip()
            elif line_lower.startswith('type:'):
                type_val = line.split(':', 1)[1].strip().lower()
                if type_val in ['verb', 'noun', 'adjective', 'adverb', 'phrase', 'other']:
                    word_data['type'] = type_val
            elif line_lower.startswith('category:'):
                cat_val = line.split(':', 1)[1].strip().lower()
                if cat_val in ['conversation', 'grammar', 'vocabulary']:
                    word_data['category'] = cat_val
            elif line_lower.startswith('explanation:'):
                word_data['explanation'] = line.split(':', 1)[1].strip()
            elif line_lower.startswith('example'):
                example_text = line.split(':', 1)[1].strip() if ':' in line else line
                if example_text:
                    examples.append(example_text)
        
        word_data['examples'] = examples
        
        if not word_data['english']:
            raise ValueError("Failed to extract English translation")
        
        return word_data
        
    except Exception as e:
        print(f"Error analyzing word: {e}")
        return {
            'german': word,
            'english': f'Error: {str(e)[:50]}',
            'russian': 'Ошибка обработки',
            'type': 'other',
            'category': 'vocabulary',
            'explanation': f'Analysis failed: {str(e)}',
            'examples': []
        }

# ==================== MONGODB API ROUTES (Updated with auth) ====================

@app.route('/api/dictionary', methods=['GET'])
@login_required
def get_dictionary():
    """Get all dictionary words for current user"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        user_id = get_current_user_id()
        words = list(dictionary_collection.find({'user_id': user_id}))
        
        for word in words:
            word['_id'] = str(word['_id'])
            word['id'] = word.get('id', str(word['_id']))
        
        return jsonify(words), 200
        
    except Exception as e:
        print(f"Error getting dictionary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dictionary', methods=['POST'])
@login_required
def add_to_dictionary():
    """Add a word to dictionary"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        data = request.json
        user_id = get_current_user_id()
        
        word_data = {
            'user_id': user_id,
            'german': data.get('german', ''),
            'english': data.get('english', ''),
            'russian': data.get('russian', ''),
            'type': data.get('type', 'other'),
            'category': data.get('category', 'vocabulary'),
            'explanation': data.get('explanation', ''),
            'examples': data.get('examples', []),
            'timestamp': datetime.now().isoformat(),
            'id': data.get('id', int(datetime.now().timestamp() * 1000))
        }
        
        result = dictionary_collection.insert_one(word_data)
        word_data['_id'] = str(result.inserted_id)
        
        return jsonify(word_data), 201
        
    except Exception as e:
        print(f"Error adding to dictionary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dictionary/<word_id>', methods=['PUT'])
@login_required
def update_dictionary_word(word_id):
    """Update a dictionary word"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        data = request.json
        user_id = get_current_user_id()
        
        query = {'user_id': user_id}
        try:
            query['id'] = int(word_id)
        except:
            query['_id'] = ObjectId(word_id)
        
        update_data = {
            'german': data.get('german'),
            'english': data.get('english'),
            'russian': data.get('russian'),
            'type': data.get('type'),
            'category': data.get('category'),
            'explanation': data.get('explanation', ''),
            'examples': data.get('examples', [])
        }
        
        result = dictionary_collection.update_one(query, {'$set': update_data})
        
        if result.modified_count > 0:
            return jsonify({"success": True}), 200
        else:
            return jsonify({"error": "Word not found"}), 404
            
    except Exception as e:
        print(f"Error updating dictionary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dictionary/<word_id>', methods=['DELETE'])
@login_required
def delete_dictionary_word(word_id):
    """Delete a dictionary word"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        user_id = get_current_user_id()
        
        query = {'user_id': user_id}
        try:
            query['id'] = int(word_id)
        except:
            query['_id'] = ObjectId(word_id)
        
        result = dictionary_collection.delete_one(query)
        
        if result.deleted_count > 0:
            return jsonify({"success": True}), 200
        else:
            return jsonify({"error": "Word not found"}), 404
            
    except Exception as e:
        print(f"Error deleting from dictionary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dictionary/sync', methods=['POST'])
@login_required
def sync_dictionary():
    """Sync local dictionary with server (merge)"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        data = request.json
        local_words = data.get('words', [])
        user_id = get_current_user_id()
        
        server_words = list(dictionary_collection.find({'user_id': user_id}))
        server_germans = {w['german']: w for w in server_words}
        
        added_count = 0
        
        for word in local_words:
            if word['german'] not in server_germans:
                word_data = {
                    'user_id': user_id,
                    'german': word['german'],
                    'english': word['english'],
                    'russian': word['russian'],
                    'type': word['type'],
                    'category': word['category'],
                    'explanation': word.get('explanation', ''),
                    'examples': word.get('examples', []),
                    'timestamp': word.get('timestamp', datetime.now().isoformat()),
                    'id': word.get('id', int(datetime.now().timestamp() * 1000))
                }
                dictionary_collection.insert_one(word_data)
                added_count += 1
        
        all_words = list(dictionary_collection.find({'user_id': user_id}))
        for word in all_words:
            word['_id'] = str(word['_id'])
            word['id'] = word.get('id', str(word['_id']))
        
        return jsonify({
            "words": all_words,
            "added_count": added_count,
            "total_count": len(all_words)
        }), 200
        
    except Exception as e:
        print(f"Error syncing dictionary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/log', methods=['GET'])
@login_required
def get_log():
    """Get learning log"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        user_id = get_current_user_id()
        logs = list(log_collection.find({'user_id': user_id}).sort('timestamp', -1).limit(100))
        
        for log in logs:
            log['_id'] = str(log['_id'])
        
        return jsonify(logs), 200
        
    except Exception as e:
        print(f"Error getting log: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/log', methods=['POST'])
@login_required
def add_to_log():
    """Add entry to learning log"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        data = request.json
        user_id = get_current_user_id()
        
        log_entry = {
            'user_id': user_id,
            'content': data.get('content', ''),
            'timestamp': datetime.now().isoformat(),
            'id': int(datetime.now().timestamp() * 1000)
        }
        
        result = log_collection.insert_one(log_entry)
        log_entry['_id'] = str(result.inserted_id)
        
        return jsonify(log_entry), 201
        
    except Exception as e:
        print(f"Error adding to log: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/log', methods=['DELETE'])
@login_required
def clear_log():
    """Clear all log entries"""
    try:
        if db is None:
            return jsonify({"error": "Database not connected"}), 500
        
        user_id = get_current_user_id()
        result = log_collection.delete_many({'user_id': user_id})
        
        return jsonify({
            "success": True,
            "deleted_count": result.deleted_count
        }), 200
        
    except Exception as e:
        print(f"Error clearing log: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== EXISTING ROUTES (Updated with auth) ====================


@app.route('/exercise', methods=['POST', 'OPTIONS'])
@login_required
def get_exercise():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.json
        topics = data.get('topics', [])
        exercise_type = data.get('exercise_type', 'translation')
        dictionary_words = data.get('dictionary_words', [])
        
        print(f"Exercise request: {exercise_type}, topics: {topics}, dict words: {len(dictionary_words)}")
        
        if not topics and not dictionary_words:
            return jsonify({"error": "No topics or dictionary words provided"}), 400
        
        question = generate_exercise(topics, exercise_type, dictionary_words)
        
        return jsonify({
            "question": question,
            "exercise_type": exercise_type,
            "topics": topics,
            "using_dictionary": len(dictionary_words) > 0,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error in get_exercise: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/check-answer', methods=['POST', 'OPTIONS'])
@login_required
def check_answer_route():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.json
        question = data.get('question', '')
        answer = data.get('answer', '')
        exercise_type = data.get('exercise_type', 'translation')
        
        if not question or not answer:
            return jsonify({"error": "Question and answer required"}), 400
        
        feedback = check_answer(question, answer, exercise_type)
        
        return jsonify({
            "feedback": feedback,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        print(f"Error checking answer: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/analyze-word', methods=['POST', 'OPTIONS'])
@login_required
def analyze_word_route():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.json
        word = data.get('word', '')
        context = data.get('context', '')
        
        if not word:
            return jsonify({"error": "Word required"}), 400
        
        word_data = analyze_word(word, context)
        
        return jsonify(word_data)
    except Exception as e:
        print(f"Error analyzing word: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    # Fixed: Use 'is not None' instead of just 'if db'
    db_status = "connected" if db is not None else "disconnected"
    return jsonify({
        "status": "healthy",
        "api_key_configured": bool(openai.api_key),
        "mongodb_status": db_status,
        "exercise_history_size": len(exercise_history),
        "timestamp": datetime.now().isoformat()
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*50}")
    print(f"German Learning App API - With Authentication")
    print(f"Port: {port}")
    print(f"OpenAI API Key: {'✓' if openai.api_key else '✗'}")
    print(f"MongoDB: {'✓ Connected' if db else '✗ Not connected'}")
    print(f"{'='*50}\n")

    app.run(host='0.0.0.0', port=port, debug=True)

