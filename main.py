from flask import Flask, render_template, request, redirect, url_for, session
from docx import Document
import pandas as pd
import io
from deep_translator import GoogleTranslator
import re
from sqlalchemy import create_engine
import math
import os
from dotenv import load_dotenv
import tempfile
import psutil

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here')

# Translation cache to reduce API calls
translation_cache = {}

def extract_table_alternative(docx_bytes):
    try:
        document = Document(io.BytesIO(docx_bytes))
        if len(document.tables) < 2:
            return None
        table = document.tables[1]  # Only process the second table
        header_row = table.rows[0]
        data_rows = [row for row in table.rows]
        if not data_rows or len(data_rows) < 2:
            return pd.DataFrame()
        num_cols = len(data_rows[1].cells)
        columns = [cell.text.strip() for cell in header_row.cells[:num_cols]]
        if not columns:
            columns = [f"Column_{i+1}" for i in range(num_cols)]
        table_data = []
        for row in data_rows[1:]:
            row_values = [cell.text for cell in row.cells]
            if len(row_values) < num_cols:
                row_values.extend([''] * (num_cols - len(row_values)))
            elif len(row_values) > num_cols:
                row_values = row_values[:num_cols]
            table_data.append(row_values)
        df = pd.DataFrame(table_data, columns=columns)
        return df
    except Exception as e:
        print(f"Error extracting table (alternative method): {e}")
        return None

def contains_devanagari(text):
    return re.search(r'[\u0900-\u097F]', str(text)) is not None

def translate_devanagari_only(text):
    if isinstance(text, str) and contains_devanagari(text):
        if text in translation_cache:
            return translation_cache[text]
        try:
            translated = GoogleTranslator(source='mr', target='en').translate(text)
            translation_cache[text] = translated
            return translated
        except Exception as e:
            print(f"Error translating '{text}': {e}")
            return text
    return text

def translate_dataframe_content(df):
    translator = GoogleTranslator(source='mr', target='en')
    # Avoid copy by using the original DataFrame
    new_columns = {col: translate_devanagari_only(col) for col in df.columns}
    df.rename(columns=new_columns, inplace=True)
    # Batch translate unique values in each column
    for col in df.columns:
        unique_values = df[col].unique()
        translations = {val: translate_devanagari_only(val) for val in unique_values}
        df[col] = df[col].map(translations)
    return df

def convert_date_format(date_str):
    try:
        pd_date = pd.to_datetime(date_str, format='%m/%d/%Y', errors='raise')
        return pd_date.strftime('%Y-%m-%d')
    except ValueError:
        try:
            pd_date = pd.to_datetime(date_str, format='%d.%m.%Y', errors='raise')
            return pd_date.strftime('%Y-%m-%d')
        except ValueError:
            return date_str

def process_data(docx_file_bytes, excluded_survey_numbers_str):
    print(f"Memory usage before processing: {psutil.Process().memory_info().rss / 1024**2:.2f} MB")
    try:
        df = extract_table_alternative(docx_file_bytes)
        if df is None:
            return "No valid table found in the document.", "", pd.DataFrame()
        if df.empty:
            return "The extracted table is empty.", "", pd.DataFrame()
        
        df.columns = df.iloc[0]
        df1 = df[1:]
        if df1.empty:
            return "No data rows found in the table.", "", pd.DataFrame()
        
        print(f"Memory usage after DataFrame creation: {psutil.Process().memory_info().rss / 1024**2:.2f} MB")
        
        # Select relevant columns, fall back to all columns if none match
        relevant_columns = ['Date', 'Purchase price', 'Per sq. M.', 'Type of document', 'Survey No.']
        available_columns = [col for col in relevant_columns if col in df1.columns]
        if not available_columns:
            # Use all columns to avoid empty DataFrame
            available_columns = df1.columns.tolist()
            print(f"Warning: None of the expected columns {relevant_columns} found. Using all columns: {available_columns}")
        df1 = df1[available_columns]
        
        translated_df1 = translate_dataframe_content(df1)
        print(f"Memory usage after translation: {psutil.Process().memory_info().rss / 1024**2:.2f} MB")
        
        if 'Date' in translated_df1.columns:
            translated_df1['Date'] = translated_df1['Date'].apply(convert_date_format)
        
        # Optimize dtypes
        if 'Per sq. M.' in translated_df1.columns:
            translated_df1['Per sq. M.'] = pd.to_numeric(translated_df1['Per sq. M.'], errors='coerce', downcast='float')
        if 'Type of document' in translated_df1.columns:
            translated_df1['Type of document'] = translated_df1['Type of document'].astype('category')
        
        # Check if DataFrame is empty or has no columns before to_sql
        if translated_df1.empty or not translated_df1.columns.tolist():
            return "No valid columns or data after processing.", "", pd.DataFrame()
        
        # Use temporary file-based SQLite database
        with tempfile.NamedTemporaryFile(suffix='.sqlite') as temp_db:
            engine = create_engine(f'sqlite:///{temp_db.name}')
            table_name = 'mytable'
            translated_df1.to_sql(table_name, engine, if_exists='replace', index=False)
            
            date_column = 'Date' if 'Date' in translated_df1.columns else None
            price_column = 'Purchase price' if 'Purchase price' in translated_df1.columns else None
            p_column = 'Per sq. M.' if 'Per sq. M.' in translated_df1.columns else None
            deed_type_column = 'Type of document' if 'Type of document' in translated_df1.columns else None
            start_date = '2020-05-02'
            end_date = '2023-05-02'
            deed_values = ['Conveyance deed', 'Convens ded', '65 Missing Letters', '65-Church letter letter', 'Conjunction', 'Contract']
            deed_values_str = ', '.join([f"'{v}'" for v in deed_values])
            
            # Build combined query only if required columns exist
            if not all([date_column, price_column, p_column, deed_type_column]):
                return "Required columns (Date, Purchase price, Per sq. M., Type of document) not found.", "", pd.DataFrame()
            
            combined_query = f"""
            SELECT * FROM {table_name}
            WHERE `{date_column}` BETWEEN '{start_date}' AND '{end_date}'
            AND `{price_column}` NOT IN (0, 1)
            AND `{p_column}` NOT BETWEEN 0 AND 10
            AND `{deed_type_column}` NOT IN ({deed_values_str})
            """
            final_table = pd.read_sql(combined_query, engine)
            final_table_name = 'final_filtered_table'
            final_table.to_sql(final_table_name, engine, if_exists='replace', index=False)
            
            sqm_column = 'Per sq. M.'
            survey_no_column = 'Survey No.' if 'Survey No.' in translated_df1.columns else None
            if not survey_no_column:
                return "Survey No. column not found.", "", pd.DataFrame()
            
            excluded_numbers = [num.strip() for num in excluded_survey_numbers_str.replace(' ', ',').split(',') if num.strip().isdigit()]
            excluded_condition = ""
            if excluded_numbers:
                excluded_numbers_str = ', '.join(map(str, excluded_numbers))
                excluded_condition = f"AND CAST(SUBSTR(`{survey_no_column}`, 1, INSTR(`{survey_no_column}`, '/') - 1) AS INTEGER) NOT IN ({excluded_numbers_str})"
            
            survey_no_filter_query = f"SELECT * FROM {final_table_name} WHERE INSTR(`{survey_no_column}`, '/') = 0 {excluded_condition} OR (INSTR(`{survey_no_column}`, '/') > 0 {excluded_condition})"
            df_filtered_survey = pd.read_sql(survey_no_filter_query, engine)
            df_filtered_survey[sqm_column] = pd.to_numeric(df_filtered_survey[sqm_column], errors='coerce')
            
            # Sort in-place to save memory
            df_filtered_survey.sort_values(by=sqm_column, ascending=False, inplace=True)
            
            half_rows = math.ceil(len(df_filtered_survey) / 2) if len(df_filtered_survey) > 0 else 0
            top_half_df = df_filtered_survey.head(half_rows)
            average_sqm = top_half_df[sqm_column].mean() if sqm_column in top_half_df.columns and not top_half_df.empty else None
            
            print(f"Memory usage after processing: {psutil.Process().memory_info().rss / 1024**2:.2f} MB")
            
            return (
                f"The average of these {half_rows} purchase and sale transactions is Rs. {average_sqm:.2f}/- per sq. m." if average_sqm
                else "Could not calculate the average 'Per sq. M.' due to missing data or column.",
                f"सदर {half_rows} खरेदी विक्री व्यवहारांची सरासरी रु. {average_sqm:.2f}/- प्रती चौ. मी." if average_sqm else "",
                top_half_df
            )
    except Exception as e:
        return f"An error occurred: {e}", "", pd.DataFrame()

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login_post():
    user_id = request.form['user_id']
    password = request.form['password']
    if user_id == 'admin' and password == '5555':
        session['logged_in'] = True
        return redirect(url_for('index'))
    return render_template('login.html', error='Invalid credentials')

@app.route('/index')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    docx_file = request.files['input_file']
    max_size_mb = 5  # Limit to 5MB
    docx_file.seek(0, os.SEEK_END)
    file_size = docx_file.tell()
    if file_size > max_size_mb * 1024 * 1024:
        return render_template('index.html', result_en="File too large. Max size is 5MB.", result_mr="")
    docx_file.seek(0)  # Reset file pointer
    excluded_survey_numbers = request.form['excluded_survey_numbers']
    result_en, result_mr, table = process_data(docx_file.read(), excluded_survey_numbers)
    return render_template('index.html', result_en=result_en, result_mr=result_mr, table=table.to_html(classes='data', header=True))

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.getenv('PORT', 5001)))
