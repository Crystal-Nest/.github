#!/bin/bash

# Example: ./workflow-update-cli.sh goblin-fabrications /compleate/path/.github 1.20.4 1.20.2 1.20.1 1.19.4 1.19.2 1.18.2

if [ "$#" -lt 3 ]; then
	echo "Usage: $0 <repository_path> <folder_path> <branch1> <branch2> ... <branchN>"
	exit 1
fi

REPO_PATH=$1
FOLDER_PATH=$2
shift 2
BRANCHES=$@

# Ensure the folder path ends with .github
if [[ ! "$FOLDER_PATH" == *.github ]]; then
	echo "Error: The folder path must end with '.github'"
	exit 1
fi

cd "$REPO_PATH" || {
	echo "Repository path not found: $REPO_PATH"
	exit 1
}

for BRANCH in $BRANCHES; do
	echo "Switching to branch: $BRANCH"
	git checkout "$BRANCH" || {
		echo "Failed to switch to branch: $BRANCH"
		continue
	}

	echo "Copying folder: $FOLDER_PATH to .github"
	# Ensure the .github directory exists and is empty
	rm -rf .github
	mkdir -p .github
	cp -rv "$FOLDER_PATH"/* .github/ || {
		echo "Failed to copy folder: $FOLDER_PATH"
		continue
	}

	echo "Adding changes"
	git add .github || {
		echo "Failed to add changes"
		continue
	}

	echo "Committing changes"
	git commit -m "Update workflows" || {
		echo "Failed to commit changes"
		continue
	}

	echo "Pushing changes"
	git push || {
		echo "Failed to push changes"
		continue
	}

	echo "Successfully updated branch: $BRANCH"
done
