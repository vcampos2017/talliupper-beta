# Publish TalliUpper to GitHub

The GitHub connector can work with repositories that already exist, but it cannot
create a new repository. Create the empty repository once, then publish this
prepared project.

## 1. Create the repository

1. Open [github.com/new](https://github.com/new).
2. Set the repository name to `talliupper`.
3. Add a short description such as “Local-first receipt OCR and structured data
   extraction in Python.”
4. Choose **Public** if this will be a portfolio project.
5. Do not add a README, `.gitignore`, or license—the project already contains all
   three.
6. Select **Create repository**.

## 2. Publish from the project folder

Open PowerShell or a terminal inside the extracted `talliupper` folder:

```bash
git init
git add .
git commit -m "Build local-first receipt OCR app"
git branch -M main
git remote add origin https://github.com/vcampos2017/talliupper.git
git push -u origin main
```

If Git asks for your identity before the commit, configure the name and email you
want associated with GitHub commits:

```bash
git config --global user.name "Your Name"
git config --global user.email "YOUR_GITHUB_EMAIL"
```

GitHub may open a browser to sign you in during the first push. Do not paste an API
key or password into project files.

## 3. Check the repository

After the push:

- confirm the sample image and README render correctly;
- open the **Actions** tab and wait for the Python 3.11 and 3.12 tests;
- add the repository URL to your profile or portfolio;
- consider opening an issue for the first roadmap feature you want to learn.

The generated database, `.env`, virtual environment, caches, and real receipt
images should remain untracked. Run `git status` before each commit and inspect
the file list carefully.
